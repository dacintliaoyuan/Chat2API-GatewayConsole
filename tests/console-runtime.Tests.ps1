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

# ---------------------------------------------------------------------------
# Lock file name sanitization (must match Python _sanitize_resource)
# ---------------------------------------------------------------------------

Assert-Equal (ConvertTo-ConsoleLockFileName -Resource 'service:chat2api') 'service_chat2api' 'colon becomes underscore in lock file name'
Assert-Equal (ConvertTo-ConsoleLockFileName -Resource 'service:M365') 'service_M365' 'case is preserved in lock file name'
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
Assert-Equal $allSnapshot.services.Count 3 'status-all covers three services (B0 retires Qoder/CLIProxy)'

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
                           CreationDate = [datetime]'2026-09-29T20:11:25' },
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
    # terminates only the private instance, leaves the 8110 share backend
    # running, and the operation record names the terminated PID.
    $script:bridgeScopeListeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 990108 })
    $script:bridgeScopeTable = @(
        [pscustomobject]@{ ProcessId = 990108; Name = 'python.exe'
                           CommandLine = 'python  chat2api.py --base-url https://chat.example.invalid'
                           CreationDate = [datetime]'2026-09-29T20:11:25' },
        [pscustomobject]@{ ProcessId = 990109; Name = 'python.exe'
                           CommandLine = 'C:\Python314\python.exe chat2api.py --base-url https://chat.example.invalid --port 8110 --profile C:\share-backend\edge-profile --login-timeout 600'
                           CreationDate = [datetime]'2026-09-29T20:14:05' }
    )
    $script:bridgeScopeTerminated.Clear()
    $scopedStop = Invoke-ConsoleRuntimeStop -Key 'chat2api' `
        -LockDir (Join-Path $env:TEMP 'console-test-locks')
    Assert-Equal $scopedStop.ok $true 'bridge reports a scoped stop as ok'
    Assert-Equal ($script:bridgeScopeTerminated -join ',') '990108' 'the console stop chain terminates only the private instance'
    Assert-Equal ($scopedStop.result.terminated_process_ids -join ',') '990108' 'the operation record names the terminated private PID'
    Assert-Equal ($script:bridgeScopeTerminated -contains 990109) $false 'the 8110 share backend survives the console stop chain'
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
$pyFile = Join-Path $tempRoot 'holder.py'
$pyCode = @'
import sys, time
sys.path.insert(0, r"__GATEWAY__")
from console_operations import ResourceFileLock
lock = ResourceFileLock(sys.argv[1], "service:testkey", "test-holder", timeout_s=2.0)
lock.acquire()
print("READY", flush=True)
time.sleep(6)
# process exit closes the handle; the lock releases without any marker cleanup
'@
$pyCode = $pyCode.Replace('__GATEWAY__', $root)
[System.IO.File]::WriteAllText((Join-Path $tempRoot 'holder.py'), $pyCode,
    [System.Text.UTF8Encoding]::new($false))
$outFile = Join-Path $tempRoot 'holder.out'
$py = Start-Process -FilePath 'python' -ArgumentList @('-u', (Join-Path $tempRoot 'holder.py'), $tempRoot) `
    -RedirectStandardOutput $outFile -PassThru -WindowStyle Hidden
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if ((Test-Path $outFile) -and ((Get-Content $outFile -Raw) -match 'READY')) {
        $ready = $true
        break
    }
}
Assert-True $ready 'python holder acquired the lock (READY)'
Assert-True (-not $py.HasExited) 'python holder still alive before contention'

# While the live Python process holds the handle, PowerShell must time out.
$contended = $false
try {
    Enter-ConsoleResourceLock -Resource 'service:testkey' -LockDir $tempRoot -TimeoutSeconds 2 | Out-Null
} catch [System.TimeoutException] {
    $contended = $true
} catch {
    $contended = $true  # any failure to acquire while held is the expected loss
}
Assert-True $contended 'PS lock acquisition fails while Python holds it'

# After the holder exits, the OS has closed its handle: acquisition succeeds.
$py.WaitForExit(10000) | Out-Null
$reacquired = Enter-ConsoleResourceLock -Resource 'service:testkey' -LockDir $tempRoot -TimeoutSeconds 5
Assert-True ($null -ne $reacquired.Stream) 'PS acquires the lock after the holder exits'
Exit-ConsoleResourceLock -Lock $reacquired

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
Assert-Equal @($parsed.services).Count 3 'status-all three services (read-only real query, B0)'

Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "$($script:failures) failure(s)" -ForegroundColor Red
    exit 1
}
Write-Host 'ALL PASS' -ForegroundColor Green
exit 0
