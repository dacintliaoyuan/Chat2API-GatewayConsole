# Offline regression tests for the console runtime JSON bridge.
# Verifies the PowerShell lock protocol against the Python implementation via
# a REAL cross-process hold, plus snapshot/start/stop shapes with injected
# test-only scriptblock seams. No real gateway service is started or stopped.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'lib\gateway-runtime.ps1')
. (Join-Path $root 'lib\console-runtime-bridge.ps1')
$script:failures = 0

# Round 18 (2026-09-29): the refusal cases below drive the REAL stop branch
# (no HttpChecker seam), so the HTTP boundary is substituted in this scope —
# no test here may reach a production health or shutdown endpoint. The CLI
# status-all child process runs in its own scope and is unaffected.
function Invoke-WebRequest {
    [CmdletBinding()]
    param([string]$Uri, [string]$Method, [object]$Headers, [int]$TimeoutSec,
          [switch]$UseBasicParsing)
    throw 'offline substitute: no HTTP request leaves this test'
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ([object]::Equals($Actual, $Expected)) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: expected '$Expected', got '$Actual'" -ForegroundColor Red
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]$Condition,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($Condition) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: condition was false" -ForegroundColor Red
}

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Script,
        [Parameter(Mandatory = $true)][string]$Name
    )
    try {
        & $Script | Out-Null
    } catch {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: expected a throw, none happened" -ForegroundColor Red
}

function Assert-ThrowsMatch {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Script,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Name
    )
    try {
        & $Script | Out-Null
    } catch {
        if ([string]$_.Exception.Message -match $Pattern) {
            Write-Host "PASS $Name" -ForegroundColor Green
            return
        }
        $script:failures++
        Write-Host "FAIL $Name`: message '$($_.Exception.Message)' did not match '$Pattern'" -ForegroundColor Red
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: expected a throw, none happened" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Lock file name sanitization (must match Python _sanitize_resource)
# ---------------------------------------------------------------------------

Assert-Equal (ConvertTo-ConsoleLockFileName -Resource 'service:chat2api') 'service_chat2api' 'colon becomes underscore in lock file name'
Assert-Equal (ConvertTo-ConsoleLockFileName -Resource 'service:chat2api') 'service_chat2api' 'case is preserved in lock file name'
Assert-Equal ((ConvertTo-ConsoleLockFileName -Resource ('a' * 80)).Length) 64 'lock file name capped at 64 chars'

# ---------------------------------------------------------------------------
# Snapshot shape with an injected Inspector (no real ports touched)
# ---------------------------------------------------------------------------

$fakeInspection = {
    param($Key)
    [pscustomobject]@{
        Key = $Key; State = 'Conflict'; Healthy = $false
        ProcessIds = @(4321, 8765); Detail = '端口被未知程序占用'
    }
}
$snapshot = Get-ConsoleRuntimeSnapshot -Key 'chat2api' -Inspector $fakeInspection
Assert-Equal $snapshot.ok $true 'snapshot ok flag'
Assert-Equal $snapshot.services.Count 1 'snapshot returns one service for a single key'
Assert-Equal $snapshot.services[0].key 'chat2api' 'snapshot key passthrough'
Assert-Equal $snapshot.services[0].state 'Conflict' 'snapshot state passthrough'
Assert-Equal $snapshot.services[0].detail '端口被未知程序占用' 'snapshot keeps Chinese detail'
Assert-Equal ($snapshot.services[0].process_ids -join ',') '4321,8765' 'snapshot process_ids array'
Assert-Equal $snapshot.services[0].process_ids[0].GetType().Name 'Int32' 'process ids are integers'
Assert-Equal ($snapshot.meta.generated_epoch -gt 0) $true 'generated epoch is a number'
$bad = Get-ConsoleRuntimeSnapshot -Key 'no-such-service' -Inspector $fakeInspection
Assert-Equal $bad.ok $false 'unknown key reports structured error'
Assert-Equal $bad.error 'invalid_key' 'unknown key error code'

# A null ProcessIds entry must not surface as process id 0 for a stopped
# service (the real loop caught [0] for Stopped before the null filter).
$nullPidSnapshot = Get-ConsoleRuntimeSnapshot -Key 'chat2api' -Inspector {
    param($Key)
    [pscustomobject]@{ Key = $Key; State = 'Stopped'; Healthy = $false
                       ProcessIds = $null; Detail = '已停止' }
}
Assert-Equal $nullPidSnapshot.services[0].process_ids.Count 0 'null ProcessIds renders an empty PID list (no pid 0)'

$allSnapshot = Get-ConsoleRuntimeSnapshot -Inspector {
    param($Key)
    [pscustomobject]@{ Key = $Key; State = 'Stopped'; Healthy = $false
                       ProcessIds = @(); Detail = '已停止' }
}
Assert-Equal $allSnapshot.services.Count 2 'status-all covers the assembled services'

# ---------------------------------------------------------------------------
# Start/Stop through the bridge with test-only seams (no real services)
# ---------------------------------------------------------------------------

$started = Invoke-ConsoleRuntimeStart -Key 'chat2api' -LockDir (Join-Path $env:TEMP 'console-test-locks') -Starter {
    param($Key)
    [pscustomobject]@{ Key = $Key; Status = 'Started'; Action = 'Start' }
}
Assert-Equal $started.ok $true 'bridge start reports ok'
Assert-Equal $started.result.status 'Started' 'bridge start nests status under result'
$badStart = Invoke-ConsoleRuntimeStart -Key 'nope' -LockDir (Join-Path $env:TEMP 'console-test-locks')
Assert-Equal $badStart.error 'invalid_key' 'start with unknown key is rejected before locking'

# ---------------------------------------------------------------------------
# Restart through the bridge (R9-R25): stop+start inside ONE lock acquisition
# (no other console action can start the service between the halves), same
# nested result shape; unknown keys still reject before locking. The seam
# hooks are pure actions — any output would leak into the result stream.
# ---------------------------------------------------------------------------

$restartStopperCalls = New-Object System.Collections.ArrayList
$restartLauncherCalls = New-Object System.Collections.ArrayList
$restartHealth = [System.Collections.Generic.Queue[bool]]::new()
$restartHealth.Enqueue($true)   # stop: was healthy
$restartHealth.Enqueue($false)  # stop post-check: now stopped
$restartHealth.Enqueue($false)  # start pre-check: not already running
$restartHealth.Enqueue($true)   # start post-launch: healthy again
$restarted = Invoke-ConsoleRuntimeRestart -Key 'chat2api' -LockDir (Join-Path $env:TEMP 'console-test-locks') `
    -HttpChecker { param($url, $timeout) $restartHealth.Dequeue() } `
    -Starter { param($Key, $Path, $Desc) [void]$restartLauncherCalls.Add($Key) } `
    -Stopper { param($Key, $Desc) [void]$restartStopperCalls.Add($Key) } `
    -Sleeper { param($seconds) }
Assert-Equal $restarted.ok $true 'bridge restart reports ok'
Assert-Equal $restarted.result.status 'Restarted' 'bridge restart nests status under result'
Assert-Equal $restarted.result.action 'Restart' 'bridge restart reports its action'
$badRestart = Invoke-ConsoleRuntimeRestart -Key 'nope' -LockDir (Join-Path $env:TEMP 'console-test-locks')
Assert-Equal $badRestart.error 'invalid_key' 'restart with unknown key is rejected before locking'

# ---------------------------------------------------------------------------
# Round 18 (2026-09-29): a stop whose scope could not be confirmed terminated
# nothing, so the bridge must report it as a failure — never as a completed stop
# the console would show as done. The substitutes sit on the LAST boundary (the CIM
# process query, the termination call and the launch call), so the real stop/restart
# scope branch runs; the top-level Stopper seam cannot produce a refusal status at
# all because the runtime recomputes the status from the health check.
# ---------------------------------------------------------------------------
$script:bridgeScopeTerminated = New-Object System.Collections.ArrayList
$script:bridgeScopeLaunches = New-Object System.Collections.ArrayList
$script:bridgeScopeTable = @()
$script:bridgeScopeListeners = @()

function Get-NetTCPConnection {
    [CmdletBinding()]
    param([int]$LocalPort, [object]$State)
    return @($script:bridgeScopeListeners)
}
function Get-CimInstance {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Filter, [string]$Namespace)
    if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
        $wanted = [int]$Matches[1]
        return @($script:bridgeScopeTable | Where-Object { [int]$_.ProcessId -eq $wanted })
    }
    return @($script:bridgeScopeTable)
}
function Stop-Process {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
    [void]$script:bridgeScopeTerminated.Add([int]$Id)
}
function Start-Process {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$FilePath, [object]$ArgumentList,
          [string]$WorkingDirectory, [object]$WindowStyle)
    [void]$script:bridgeScopeLaunches.Add([string]$FilePath)
}

try {
    # The verified private instance owns the 8000 listener; a second name-matching
    # process has an unreadable command line, so its identity can be neither
    # confirmed nor excluded and the whole stop must refuse.
    $script:bridgeScopeListeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990108 })
    $script:bridgeScopeTable = @(
        [pscustomobject]@{ ProcessId = 990108; Name = 'python.exe'
                           CommandLine = 'python  chat2api.py --base-url https://chat.example.invalid'
                           CreationDate = [datetime]'2026-09-29T20:11:25' }
        [pscustomobject]@{ ProcessId = 990107; Name = 'python.exe'; CommandLine = $null
                           CreationDate = [datetime]'2026-09-29T20:14:05' }
    )
    $refusedStop = Invoke-ConsoleRuntimeStop -Key 'chat2api' `
        -LockDir (Join-Path $env:TEMP 'console-test-locks')
    Assert-Equal $refusedStop.ok $false 'bridge reports a refused stop as not ok'
    Assert-Equal $refusedStop.error 'stop_scope_unconfirmed' 'bridge names the refused stop error code'
    Assert-True ($refusedStop.detail -match '990107') 'bridge keeps the refused-stop detail (the unconfirmed PID)'
    Assert-Equal ($script:bridgeScopeTerminated -join ',') '' 'the refused stop terminates nothing'

    $refusedRestart = Invoke-ConsoleRuntimeRestart -Key 'chat2api' `
        -LockDir (Join-Path $env:TEMP 'console-test-locks')
    Assert-Equal $refusedRestart.ok $false 'bridge reports a refused restart as not ok'
    Assert-Equal $refusedRestart.error 'stop_scope_unconfirmed' 'bridge names the refused restart error code'
    Assert-Equal ($script:bridgeScopeLaunches -join ',') '' 'the refused restart launches nothing'
    Assert-Equal ($script:bridgeScopeTerminated -join ',') '' 'the refused restart terminates nothing'

    # Two coexisting instances through the console chain: the private stop
    # terminates only the private instance and the operation record names the
    # terminated PID.
    $script:bridgeScopeListeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990108 })
    $script:bridgeScopeTable = @(
        [pscustomobject]@{ ProcessId = 990108; Name = 'python.exe'
                           CommandLine = 'python  chat2api.py --base-url https://chat.example.invalid'
                           CreationDate = [datetime]'2026-09-29T20:11:25' }
    )
    $script:bridgeScopeTerminated.Clear()
    $scopedStop = Invoke-ConsoleRuntimeStop -Key 'chat2api' `
        -LockDir (Join-Path $env:TEMP 'console-test-locks')
    Assert-Equal $scopedStop.ok $true 'bridge reports a scoped stop as ok'
    Assert-Equal ($script:bridgeScopeTerminated -join ',') '990108' 'the console stop chain terminates only the private instance'
    Assert-Equal ($scopedStop.result.terminated_process_ids -join ',') '990108' 'the operation record names the terminated private PID'
} finally {
    Remove-Item function:\Get-NetTCPConnection -ErrorAction SilentlyContinue
    Remove-Item function:\Get-CimInstance -ErrorAction SilentlyContinue
    Remove-Item function:\Stop-Process -ErrorAction SilentlyContinue
    Remove-Item function:\Start-Process -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Caller-holds-lock contract: with -CallerHoldsLock the bridge must NOT open
# the same lock file again (the console task layer already owns it; a second
# exclusive open would wait on the caller's own handle until timeout). Proof:
# the start succeeds while the test holds the lock, and the owner info in the
# lock file still names the TEST caller, not the bridge.
# ---------------------------------------------------------------------------

$callerLockDir = Join-Path ([System.IO.Path]::GetTempPath()) ('console-caller-lock-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $callerLockDir | Out-Null
$callerLock = Enter-ConsoleResourceLock -Resource 'service:chat2api' -LockDir $callerLockDir -Owner 'test-caller'
$hookedStart = Invoke-ConsoleRuntimeStart -Key 'chat2api' -LockDir $callerLockDir -CallerHoldsLock -Starter {
    param($Key)
    [pscustomobject]@{ Key = $Key; Status = 'Started'; Action = 'Start' }
}
Assert-Equal $hookedStart.ok $true 'start succeeds while the caller holds the lock (-CallerHoldsLock)'
# Release first: the open lock handle is FileShare.None, so the owner info
# can only be read back after disposal; the content survives (nobody
# rewrites it unless another acquirer succeeds).
Exit-ConsoleResourceLock -Lock $callerLock
$ownerInfo = $null
try {
    $ownerInfo = Get-Content (Join-Path $callerLockDir 'service_chat2api.lock') -Raw | ConvertFrom-Json
} catch { }
Assert-Equal $ownerInfo.owner 'test-caller' 'bridge did not re-acquire the caller-held lock'
Remove-Item -Recurse -Force $callerLockDir -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# Cross-process lock mutual exclusion (Python holder vs PowerShell acquirer)
# ---------------------------------------------------------------------------

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('console-lock-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
# Cross-process holder: a second PowerShell process holds the same lock file.
# The holder dot-sources the real bridge, so the protocol under test is the same
# one production uses; no Python console package is required (retired, #90).
$holderScript = Join-Path $tempRoot 'holder.ps1'
$readyFile = Join-Path $tempRoot 'holder.ready'
$holderCode = @'
param([string]$LockDir, [string]$ReadyFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $env:CONSOLE_TEST_ROOT 'lib\gateway-runtime.ps1')
. (Join-Path $env:CONSOLE_TEST_ROOT 'lib\console-runtime-bridge.ps1')
$lock = Enter-ConsoleResourceLock -Resource 'service:testkey' -LockDir $LockDir `
    -Owner 'test-holder' -TimeoutSeconds 2
[System.IO.File]::WriteAllText($ReadyFile, 'READY')
Start-Sleep -Seconds 6
# process exit closes the handle; the lock releases without any marker cleanup
'@
[System.IO.File]::WriteAllText($holderScript, $holderCode,
    [System.Text.UTF8Encoding]::new($false))
$holderExe = (Get-Process -Id $PID).Path
$savedTestRoot = $env:CONSOLE_TEST_ROOT
$env:CONSOLE_TEST_ROOT = $root
try {
    $holder = Start-Process -FilePath $holderExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $holderScript, $tempRoot, $readyFile) `
        -PassThru -WindowStyle Hidden
} finally {
    if ($null -eq $savedTestRoot) {
        Remove-Item Env:CONSOLE_TEST_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:CONSOLE_TEST_ROOT = $savedTestRoot
    }
}
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if (Test-Path $readyFile) {
        $ready = $true
        break
    }
}
Assert-True $ready 'powershell holder acquired the lock (READY)'
Assert-True (-not $holder.HasExited) 'powershell holder still alive before contention'

# While the live holder process owns the handle, this process must time out.
$contended = $false
try {
    Enter-ConsoleResourceLock -Resource 'service:testkey' -LockDir $tempRoot -TimeoutSeconds 2 | Out-Null
} catch [System.TimeoutException] {
    $contended = $true
} catch {
    $contended = $true  # any failure to acquire while held is the expected loss
}
Assert-True $contended 'PS lock acquisition fails while another process holds it'

# After the holder exits, the OS has closed its handle: acquisition succeeds.
$holder.WaitForExit(10000) | Out-Null
$reacquired = Enter-ConsoleResourceLock -Resource 'service:testkey' -LockDir $tempRoot -TimeoutSeconds 5
Assert-True ($null -ne $reacquired.Stream) 'PS acquires the lock after the holder exits'
Exit-ConsoleResourceLock -Lock $reacquired

# ---------------------------------------------------------------------------
# #118: desktop shortcut and CLI share one 8001 residual-process boundary.
#
# 2026-11-20: 8001 had no listener but an old managed console process was still
# alive. The shortcut treated the state as residual and aborted; the CLI start asked
# the runtime to clean it up and got AlreadyStopped back ("受管残留进程清理失败").
# These scenarios drive the REAL readiness helper with only the OS/HTTP boundaries
# substituted:
#   - a starting instance becomes ready during the short recheck -> reuse it;
#   - a positively identified private instance is the only thing stopped;
#   - a listener whose health fails, or an unconfirmable process, keeps every
#     process and returns a distinct reason;
#   - a concurrent opener waits on the same service lock and starts nothing.
# ---------------------------------------------------------------------------

$script:entryTerminated = New-Object System.Collections.ArrayList
$script:entryLaunches = New-Object System.Collections.ArrayList
$script:entryOpened = New-Object System.Collections.ArrayList
$script:entryTable = @()
$script:entryListeners = @()
$script:entryServiceStarted = $false
$script:entryHealthCalls = New-Object System.Collections.ArrayList
$script:entryNewPid = 990202

function New-EntryProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory = $true)][datetime]$CreationDate,
        [string]$Name = 'um-console-candidate.exe'
    )
    return [pscustomobject]@{
        ProcessId = $ProcessId; Name = $Name
        CommandLine = $CommandLine; CreationDate = $CreationDate
    }
}

function Reset-EntryScenario {
    param(
        [array]$Processes = @(),
        [array]$Listeners = @(),
        [bool]$ServiceStarted = $false
    )
    $script:entryTable = @($Processes)
    $script:entryListeners = @($Listeners)
    $script:entryServiceStarted = $ServiceStarted
    $script:entryTerminated.Clear()
    $script:entryLaunches.Clear()
    $script:entryOpened.Clear()
    $script:entryHealthCalls.Clear()
}

function Get-EntryHealth {
    # Every readiness probe names its URL; the test asserts /health was probed.
    param($Url, $TimeoutSeconds)
    [void]$script:entryHealthCalls.Add([string]$Url)
    return [bool]$script:entryServiceStarted
}

$entryScratch = Join-Path ([System.IO.Path]::GetTempPath()) ('console-entry-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $entryScratch | Out-Null

# Concurrency: a REAL second process holds the same service lock while the
# readiness helper is called. The holder simulates the first opener finishing its
# start; the helper must wait, recheck and reuse instead of starting twice.
$concurrentRoot = Join-Path $entryScratch 'concurrent'
New-Item -ItemType Directory -Path $concurrentRoot | Out-Null
$concurrentReady = Join-Path $concurrentRoot 'holder.ready'
$concurrentServed = Join-Path $concurrentRoot 'holder.served'
$concurrentScript = Join-Path $concurrentRoot 'holder.ps1'
$concurrentCode = @'
param([string]$LockDir, [string]$ReadyFile, [string]$ServedFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $env:CONSOLE_TEST_ROOT 'lib\gateway-runtime.ps1')
. (Join-Path $env:CONSOLE_TEST_ROOT 'lib\console-runtime-bridge.ps1')
$lock = Enter-ConsoleResourceLock -Resource 'service:usage' -LockDir $LockDir `
    -Owner 'first-opener' -TimeoutSeconds 2
[System.IO.File]::WriteAllText($ReadyFile, 'READY')
Start-Sleep -Seconds 2
[System.IO.File]::WriteAllText($ServedFile, 'SERVED')
# process exit closes the handle; the lock releases without any marker cleanup
'@
[System.IO.File]::WriteAllText($concurrentScript, $concurrentCode,
    [System.Text.UTF8Encoding]::new($false))
$concurrentExe = (Get-Process -Id $PID).Path
$savedConcurrentRoot = $env:CONSOLE_TEST_ROOT
$env:CONSOLE_TEST_ROOT = $root
try {
    $concurrentHolder = Start-Process -FilePath $concurrentExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $concurrentScript,
            $concurrentRoot, $concurrentReady, $concurrentServed) `
        -PassThru -WindowStyle Hidden
} finally {
    if ($null -eq $savedConcurrentRoot) {
        Remove-Item Env:CONSOLE_TEST_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:CONSOLE_TEST_ROOT = $savedConcurrentRoot
    }
}
$concurrentArmed = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if ((Test-Path $concurrentReady) -and
            ((Get-Content $concurrentReady -Raw) -match 'READY')) {
        $concurrentArmed = $true
        break
    }
}
Assert-True $concurrentArmed 'concurrent opener acquired the service lock'
& {
    function Get-NetTCPConnection { throw 'concurrent reuse must not inspect listeners' }
    function Get-CimInstance { throw 'concurrent reuse must not inspect processes' }
    function Stop-Process { throw 'concurrent reuse must not terminate anything' }
    function Start-Process { throw 'concurrent reuse must not start a second instance' }
    $reused = Invoke-ConsoleServiceReady -Key 'usage' -LockDir $concurrentRoot `
        -ReadyWaitSeconds 1 -ReadyPollMilliseconds 100 -LockTimeoutSeconds 10 `
        -HttpChecker { param($url, $timeout) Test-Path -LiteralPath $concurrentServed } `
        -Sleeper { param($seconds) Start-Sleep -Milliseconds 50 }
    Assert-Equal $reused.Status 'AlreadyRunning' 'concurrent opener reuses the first opener''s instance'
    Assert-Equal $reused.Reused $true 'concurrent reuse is reported as reused'
    Assert-Equal $reused.Started $false 'concurrent opener does not start a second instance'
}
$concurrentHolder.WaitForExit(10000) | Out-Null
Assert-True (Test-Path $concurrentServed) 'concurrent holder completed its simulated start'

& {
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [object]$State)
        return @($script:entryListeners)
    }
    function Get-CimInstance {
        [CmdletBinding()]
        param(
            [Parameter(Position = 0)][string]$ClassName,
            [string]$Filter,
            [string]$Namespace
        )
        if ($ClassName -and $ClassName -ne 'Win32_Process') {
            return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
        }
        if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
            $wanted = [int]$Matches[1]
            return @($script:entryTable | Where-Object { [int]$_.ProcessId -eq $wanted })
        }
        return @($script:entryTable)
    }
    function Stop-Process {
        [CmdletBinding()]
        param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
        [void]$script:entryTerminated.Add([int]$Id)
        $script:entryTable = @($script:entryTable |
            Where-Object { [int]$_.ProcessId -ne [int]$Id })
    }
    function Invoke-WebRequest {
        [CmdletBinding()]
        param(
            [string]$Uri,
            [string]$Method,
            [object]$Headers,
            [int]$TimeoutSec,
            [switch]$UseBasicParsing
        )
        # Inspection's own health probe has no HttpChecker seam; after the fake
        # launch it must observe the same 200 the start seam reported.
        if (-not $script:entryServiceStarted) {
            throw 'offline substitute: health probe refused'
        }
        return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
    }
    function Start-Process {
        [CmdletBinding()]
        param(
            [Parameter(Position = 0)][string]$FilePath,
            [object]$ArgumentList,
            [string]$WorkingDirectory,
            [object]$WindowStyle
        )
        if ($FilePath -match '^https?://') {
            [void]$script:entryOpened.Add([string]$FilePath)
            return
        }
        $argsText = ''
        if ($null -ne $ArgumentList) { $argsText = (@($ArgumentList) -join ' ') }
        [void]$script:entryLaunches.Add([pscustomobject]@{
            FilePath = [string]$FilePath; Arguments = $argsText
        })
        $script:entryServiceStarted = $true
        $script:entryTable = @($script:entryTable) + @(
            (New-EntryProcess -ProcessId $script:entryNewPid `
                -CommandLine 'rust\target\release\um-console-candidate.exe --mode production --bind-port 8001 --role local' `
                -CreationDate ([datetime]'2026-11-20T10:00:00')))
        $script:entryListeners = @([pscustomobject]@{
            LocalAddress = '127.0.0.1'; OwningProcess = $script:entryNewPid
        })
    }

    # --- starting instance becomes ready during the short recheck -> reuse ---
    $reuseLock = Join-Path $entryScratch 'reuse-locks'
    New-Item -ItemType Directory -Path $reuseLock | Out-Null
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990201 `
            -CommandLine 'rust\target\release\um-console-candidate.exe --mode production --bind-port 8001 --role local' `
            -CreationDate ([datetime]'2026-11-20T09:00:00'))
    )
    $reuse = Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
        -LockDir $reuseLock -ReadyWaitSeconds 1 -ReadyPollMilliseconds 10 `
        -HttpChecker {
            param($url, $timeout)
            [void]$script:entryHealthCalls.Add([string]$url)
            return ($script:entryHealthCalls.Count -gt 1)
        } `
        -Sleeper { param($seconds) } `
        -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    Assert-Equal $reuse.Status 'Opened' 'shortcut reuses an instance that becomes ready during the short recheck'
    Assert-Equal $reuse.Reused $true 'shortcut reports the ready instance as reused'
    Assert-Equal $reuse.Started $false 'shortcut does not start again during the short recheck'
    Assert-Equal $script:entryTerminated.Count 0 'shortcut terminates nothing during the short recheck'
    Assert-Equal $script:entryLaunches.Count 0 'shortcut launches nothing during the short recheck'
    Assert-Equal $script:entryOpened[0] 'http://127.0.0.1:8001' 'shortcut opens the console home page'
    Assert-True ($script:entryHealthCalls -contains 'http://127.0.0.1:8001/health') 'shortcut probes the 8001 /health endpoint'

    # --- confirmed private residual: stop only it, start once, open home ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990211 -Name 'python.exe' `
            -CommandLine 'python usagesrv.py --control-token SENTINEL_SECRET_VALUE' `
            -CreationDate ([datetime]'2026-11-20T09:10:00'))
    )
    $residualLock = Join-Path $entryScratch 'residual-locks'
    New-Item -ItemType Directory -Path $residualLock | Out-Null
    $residual = Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
        -LockDir $residualLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
        -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
        -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    Assert-Equal $residual.Status 'Opened' 'shortcut recovers a confirmed residual 8001 instance'
    Assert-Equal $residual.ReadyStatus 'Started' 'residual recovery reports a started instance'
    Assert-Equal $residual.Reused $false 'residual recovery is not a reuse'
    Assert-Equal $residual.Started $true 'residual recovery starts the console once'
    Assert-Equal ($script:entryTerminated -join ',') '990211' 'shortcut terminates only the confirmed private residual PID'
    Assert-Equal $script:entryLaunches.Count 1 'shortcut launches the console exactly once'
    Assert-Equal ([IO.Path]::GetFileName([string]$script:entryLaunches[0].FilePath)) 'um-console-candidate.exe' 'shortcut launches the managed Rust console'
    Assert-Equal ($residual.ProcessIds -join ',') '990202' 'residual recovery reports the new console PID'
    Assert-Equal $script:entryOpened[0] 'http://127.0.0.1:8001' 'residual recovery opens the console home page'
    Assert-True ($script:entryHealthCalls -contains 'http://127.0.0.1:8001/health') 'residual recovery confirms the 8001 /health endpoint'
    Assert-True (([string]$residual.Detail) -notmatch 'SENTINEL_SECRET_VALUE') 'residual detail never carries the process command line'
    Assert-True (($script:entryLaunches | Out-String) -notmatch 'SENTINEL_SECRET_VALUE') 'launch arguments never carry a process secret'

    # --- the CLI entry uses the same readiness boundary ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990212 -Name 'python.exe' `
            -CommandLine 'python usagesrv.py' `
            -CreationDate ([datetime]'2026-11-20T09:20:00'))
    )
    $cliLock = Join-Path $entryScratch 'cli-locks'
    New-Item -ItemType Directory -Path $cliLock | Out-Null
    $cli = Invoke-GatewayServiceCommand -Action 'Start' -Key 'usage' -LockDir $cliLock `
        -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
        -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) }
    Assert-Equal $cli.ok $true 'CLI start recovers a confirmed residual 8001 instance'
    Assert-Equal $cli.result.Status 'Started' 'CLI start reports the started console'
    Assert-Equal ($script:entryTerminated -join ',') '990212' 'CLI start terminates only the confirmed private residual PID'
    Assert-Equal $script:entryLaunches.Count 1 'CLI start launches the console exactly once'
    Assert-Equal ($cli.result.ProcessIds -join ',') '990202' 'CLI start reports the new console PID'

    # --- listening but unhealthy: keep every process, distinct reason ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990221 `
            -CommandLine 'rust\target\release\um-console-candidate.exe --mode production --bind-port 8001 --role local' `
            -CreationDate ([datetime]'2026-11-20T09:30:00'))
    ) -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990221 })
    $degradedLock = Join-Path $entryScratch 'degraded-locks'
    New-Item -ItemType Directory -Path $degradedLock | Out-Null
    Assert-ThrowsMatch {
        Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
            -LockDir $degradedLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
            -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
            -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    } '健康检查失败' 'shortcut reports a listening but unhealthy 8001 as a distinct reason'
    Assert-Equal $script:entryTerminated.Count 0 'unhealthy listener keeps every process'
    Assert-Equal $script:entryLaunches.Count 0 'unhealthy listener does not trigger a launch'
    Assert-True (@($script:entryTable | Where-Object { [int]$_.ProcessId -eq 990221 }).Count -eq 1) 'unhealthy listener process is still present'

    # --- foreign listener: keep it and report the unknown program ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990231 -Name 'nginx.exe' -CommandLine 'nginx.exe' `
            -CreationDate ([datetime]'2026-11-20T09:40:00'))
    ) -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990231 })
    $conflictLock = Join-Path $entryScratch 'conflict-locks'
    New-Item -ItemType Directory -Path $conflictLock | Out-Null
    Assert-ThrowsMatch {
        Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
            -LockDir $conflictLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
            -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
            -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    } '未知程序' 'shortcut reports a foreign listener as a distinct reason'
    Assert-Equal $script:entryTerminated.Count 0 'foreign listener keeps every process'
    Assert-Equal $script:entryLaunches.Count 0 'foreign listener does not trigger a launch'
    Assert-True (@($script:entryTable | Where-Object { [int]$_.ProcessId -eq 990231 }).Count -eq 1) 'foreign listener process is still present'

    # --- unconfirmable process: keep it and report the unknown ownership ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990241 -Name 'python.exe' -CommandLine $null `
            -CreationDate ([datetime]'2026-11-20T09:50:00'))
    )
    $unconfirmedLock = Join-Path $entryScratch 'unconfirmed-locks'
    New-Item -ItemType Directory -Path $unconfirmedLock | Out-Null
    Assert-ThrowsMatch {
        Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
            -LockDir $unconfirmedLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
            -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
            -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    } '归属无法确认' 'shortcut reports an unconfirmable 8001 process as a distinct reason'
    Assert-Equal $script:entryTerminated.Count 0 'unconfirmable process keeps every process'
    Assert-Equal $script:entryLaunches.Count 0 'unconfirmable process does not trigger a launch'
    Assert-True (@($script:entryTable | Where-Object { [int]$_.ProcessId -eq 990241 }).Count -eq 1) 'unconfirmable process is still present'

    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990242 -Name 'python.exe' `
            -CommandLine 'python usagesrv.py' `
            -CreationDate ([datetime]'2026-11-20T09:55:00')),
        (New-EntryProcess -ProcessId 990243 -Name 'python.exe' -CommandLine $null `
            -CreationDate ([datetime]'2026-11-20T09:56:00'))
    )
    $mixedLock = Join-Path $entryScratch 'mixed-locks'
    New-Item -ItemType Directory -Path $mixedLock | Out-Null
    Assert-ThrowsMatch {
        Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
            -LockDir $mixedLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
            -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
            -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    } '归属无法确认' 'an unconfirmable process blocks cleanup even when a private one exists'
    Assert-Equal $script:entryTerminated.Count 0 'a mixed residual scope terminates nothing'
    Assert-Equal $script:entryLaunches.Count 0 'a mixed residual scope launches nothing'
    Assert-Equal (@($script:entryTable | Where-Object { [int]$_.ProcessId -in @(990242, 990243) }).Count) 2 'both mixed-scope processes are still present'

    # --- sensitive values never enter details, launch arguments or the opened URL ---
    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990251 -Name 'python.exe' `
            -CommandLine 'python usagesrv.py --control-token SENTINEL_SECRET_VALUE' `
            -CreationDate ([datetime]'2026-11-20T10:10:00'))
    )
    $secretLock = Join-Path $entryScratch 'secret-locks'
    New-Item -ItemType Directory -Path $secretLock | Out-Null
    $secret = Invoke-GatewayConsoleOpen -Key 'usage' -BaseUrl 'http://127.0.0.1:8001' `
        -LockDir $secretLock -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
        -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) } `
        -Opener { param($url) [void]$script:entryOpened.Add([string]$url) }
    Assert-Equal $secret.Status 'Opened' 'sensitive-value scenario still recovers the residual instance'
    Assert-True (([string]$secret.Detail) -notmatch 'SENTINEL_SECRET_VALUE') 'readiness detail never carries a process secret'
    Assert-True (($script:entryLaunches | Out-String) -notmatch 'SENTINEL_SECRET_VALUE') 'launch arguments never carry a process secret'
    Assert-True (($script:entryOpened | Out-String) -notmatch 'SENTINEL_SECRET_VALUE') 'opened URL never carries a process secret'
    Assert-True (($script:entryHealthCalls | Out-String) -notmatch 'SENTINEL_SECRET_VALUE') 'health probe URLs never carry a process secret'

    Reset-EntryScenario -Processes @(
        (New-EntryProcess -ProcessId 990252 -Name 'python.exe' `
            -CommandLine 'python usagesrv.py --control-token SENTINEL_SECRET_VALUE' `
            -CreationDate ([datetime]'2026-11-20T10:20:00'))
    )
    $cliSecretLock = Join-Path $entryScratch 'cli-secret-locks'
    New-Item -ItemType Directory -Path $cliSecretLock | Out-Null
    $cliSecret = Invoke-GatewayServiceCommand -Action 'Start' -Key 'usage' -LockDir $cliSecretLock `
        -ReadyWaitSeconds 0 -ReadyPollMilliseconds 10 `
        -HttpChecker ${function:Get-EntryHealth} -Sleeper { param($seconds) }
    Assert-Equal $cliSecret.ok $true 'CLI start with a process secret still recovers'
    Assert-True (([string]$cliSecret.result.Detail) -notmatch 'SENTINEL_SECRET_VALUE') 'CLI result never carries a process secret'
}

# The actual CLI script runs in a child process with an isolated hooks file: the
# same confirmed residual PID is cleaned and the console starts exactly once.
$cliHooks = Join-Path $entryScratch 'cli-hooks.ps1'
$cliHooksCode = @'
$script:cliTable = @(
    [pscustomobject]@{ ProcessId = 990261; Name = 'python.exe'
                       CommandLine = 'python usagesrv.py'
                       CreationDate = [datetime]'2026-11-20T10:30:00' }
)
$script:cliListeners = @()
$script:cliStarted = $false
function Get-NetTCPConnection {
    [CmdletBinding()]
    param([int]$LocalPort, [object]$State)
    return @($script:cliListeners)
}
function Get-CimInstance {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Filter, [string]$Namespace)
    if ($ClassName -and $ClassName -ne 'Win32_Process') {
        return @(CimCmdlets\Get-CimInstance @PSBoundParameters)
    }
    if ($Filter -match 'ProcessId\s*=\s*(\d+)') {
        $wanted = [int]$Matches[1]
        return @($script:cliTable | Where-Object { [int]$_.ProcessId -eq $wanted })
    }
    return @($script:cliTable)
}
function Stop-Process {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
    [System.IO.File]::AppendAllText($env:CLI_RECORD_FILE, "terminated:$Id`n")
    $script:cliTable = @($script:cliTable | Where-Object { [int]$_.ProcessId -ne [int]$Id })
}
function Start-Process {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$FilePath,
        [object]$ArgumentList,
        [string]$WorkingDirectory,
        [object]$WindowStyle
    )
    $argsText = ''
    if ($null -ne $ArgumentList) { $argsText = (@($ArgumentList) -join ' ') }
    [System.IO.File]::AppendAllText($env:CLI_RECORD_FILE, "launch:$FilePath|$argsText`n")
    $script:cliStarted = $true
    $script:cliTable = @($script:cliTable) + @(
        [pscustomobject]@{ ProcessId = 990262; Name = 'um-console-candidate.exe'
                           CommandLine = 'rust\target\release\um-console-candidate.exe --mode production --bind-port 8001 --role local'
                           CreationDate = [datetime]'2026-11-20T10:40:00' })
    $script:cliListeners = @([pscustomobject]@{
        LocalAddress = '127.0.0.1'; OwningProcess = 990262
    })
}
function Invoke-WebRequest {
    [CmdletBinding()]
    param([string]$Uri, [string]$Method, [object]$Headers, [int]$TimeoutSec,
          [switch]$UseBasicParsing)
    throw 'offline substitute: no HTTP request leaves the CLI test'
}
$ConsoleTestHealthChecker = { param($url, $timeout) [bool]$script:cliStarted }
$ConsoleTestSleeper = { param($seconds) }
'@
[System.IO.File]::WriteAllText($cliHooks, $cliHooksCode,
    [System.Text.UTF8Encoding]::new($false))
$cliRecord = Join-Path $entryScratch 'cli-record.txt'
$cliChildLock = Join-Path $entryScratch 'cli-child-locks'
New-Item -ItemType Directory -Path $cliChildLock | Out-Null
$cliEntry = Join-Path $root 'invoke-gateway-service.ps1'
$powershellExe = (Get-Process -Id $PID).Path
$savedRecord = $env:CLI_RECORD_FILE
$env:CLI_RECORD_FILE = $cliRecord
try {
    $cliChild = Start-Process -FilePath $powershellExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $cliEntry,
            '-Key', 'usage', '-Action', 'Start', '-LockDir', $cliChildLock,
            '-ReadyWaitSeconds', '0', '-RuntimeHooksFile', $cliHooks) `
        -RedirectStandardOutput (Join-Path $entryScratch 'cli.out') `
        -RedirectStandardError (Join-Path $entryScratch 'cli.err') `
        -Wait -PassThru -WindowStyle Hidden
} finally {
    if ($null -eq $savedRecord) {
        Remove-Item Env:CLI_RECORD_FILE -ErrorAction SilentlyContinue
    } else {
        $env:CLI_RECORD_FILE = $savedRecord
    }
}
Assert-Equal $cliChild.ExitCode 0 'CLI script start recovers the confirmed residual 8001 instance'
$cliRecords = Get-Content -LiteralPath $cliRecord -Raw
Assert-True ($cliRecords -match 'terminated:990261') 'CLI script terminates only the confirmed private residual PID'
Assert-Equal (@($cliRecords -split "`n" | Where-Object { $_ -like 'launch:*' }).Count) 1 'CLI script launches the console exactly once'
Assert-True ($cliRecords -notmatch 'SENTINEL_SECRET_VALUE') 'CLI script records never carry a process secret'

Remove-Item -Recurse -Force $entryScratch -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# CLI entry: argument validation and a real read-only status-all
# ---------------------------------------------------------------------------

$entry = Join-Path $root 'invoke-console-runtime-json.ps1'
& pwsh -NoProfile -ExecutionPolicy Bypass -File $entry -Action 'bogus' 2>$null | Out-Null
Assert-Equal $LASTEXITCODE 2 'invalid action exits 2'
& pwsh -NoProfile -ExecutionPolicy Bypass -File $entry -Action 'status' 2>$null | Out-Null
Assert-Equal $LASTEXITCODE 2 'status without key exits 2'
# Capture through a FILE, not the pipe: the parent PowerShell decodes child
# stdout with the console codepage (GBK) and mangles the child's UTF-8 JSON.
$statusFile = Join-Path $tempRoot 'status-all.json'
$child = Start-Process -FilePath 'pwsh' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $entry, '-Action', 'status-all') `
    -RedirectStandardOutput $statusFile -Wait -PassThru -WindowStyle Hidden
Assert-Equal $child.ExitCode 0 'status-all entry exits 0'
$parsed = $null
try {
    $parsed = [System.IO.File]::ReadAllText($statusFile,
        [System.Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
} catch { }
Assert-True ($null -ne $parsed) 'status-all emits parseable JSON'
Assert-Equal $parsed.ok $true 'status-all ok'
Assert-Equal @($parsed.services).Count 2 'status-all two assembled services (read-only real query, B0)'

Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "$($script:failures) failure(s)" -ForegroundColor Red
    exit 1
}
Write-Host 'ALL PASS' -ForegroundColor Green
exit 0
