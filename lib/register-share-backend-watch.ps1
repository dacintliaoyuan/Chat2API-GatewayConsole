[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Register', 'Unregister')]
    [string]$Action = 'Register',
    [string]$TaskName = 'AI Gateway Public Share Backend Watch',
    [switch]$SelfTest
)

# Registration entry for the dedicated share backend (8110) watch - contract
# Rev9 section 7.5. It records the FIXED paths and the current identity, never
# a credential; the task itself only runs the hidden VBS launcher, and -SelfTest
# only CONSTRUCTS the task object (no registration, no ACL change, no recovery
# enablement).
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:WatchScript = Join-Path $PSScriptRoot 'watch-share-backend.ps1'
$script:HiddenLauncher = Join-Path $PSScriptRoot 'watch-share-backend-hidden.vbs'
$script:WscriptExe = Join-Path $env:SystemRoot 'System32\wscript.exe'
$script:FailureCount = 0

function Assert-SelfTestEqual {
    param($Actual, $Expected, [string]$Name)
    if ([object]::Equals($Actual, $Expected)) {
        Write-Host "PASS $Name"
        return
    }
    $script:FailureCount += 1
    Write-Host ("FAIL {0}: expected [{1}], got [{2}]" -f
        $Name, $Expected, $Actual) -ForegroundColor Red
}

function New-ShareBackendWatchTask {
    # Build the scheduled-task object. The action goes through wscript and the
    # hidden VBS launcher (a once-a-minute task must not flash a console
    # window); the VBS waits for the watch and returns its exit code.
    $arguments = '//B //Nologo "{0}"' -f $script:HiddenLauncher
    $taskAction = New-ScheduledTaskAction -Execute $script:WscriptExe `
        -Argument $arguments

    # One persistent trigger with a one-minute repetition interval; the task
    # survives reboots and StartWhenAvailable catches a missed repetition.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1)

    # The OPERATOR's own identity - never SYSTEM: SYSTEM would start a second
    # UM browser/profile outside the operator session.
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $currentIdentity `
        -LogonType Interactive -RunLevel Limited

    # IgnoreNew: never two watch runs at once. The five-minute limit bounds
    # the watch run itself; a started 8110 keeps running after the task ends.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    return New-ScheduledTask -Action $taskAction -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description ('Dedicated share backend (8110) watch, every 60s: starts only a ' +
            'vanished dedicated backend whose operator intent is "running" for this ' +
            'boot, after two confirmations and within the hourly budget; never after ' +
            'a console stop, never for a live instance, never a whole-chain restart. ' +
            'Log: %LOCALAPPDATA%\AIGatewayPublic\watch\share-backend-watch.log')
}

function Invoke-RegisterSelfTest {
    # Assert the constructed task object only; nothing is registered.
    if (-not (Test-Path -LiteralPath $script:WatchScript)) {
        throw "watch script not found: $($script:WatchScript)"
    }
    $task = New-ShareBackendWatchTask
    Assert-SelfTestEqual $task.Actions.Count 1 'exactly one action'
    Assert-SelfTestEqual $task.Actions[0].Execute $script:WscriptExe `
        'the action runs wscript.exe'
    Assert-SelfTestEqual (
        [string]$task.Actions[0].Arguments -eq
            ('//B //Nologo "{0}"' -f $script:HiddenLauncher)) $true `
        'the action arguments name the fixed hidden launcher'
    Assert-SelfTestEqual $task.Triggers.Count 1 'exactly one trigger'
    Assert-SelfTestEqual (
        [string]$task.Triggers[0].Repetition.Interval -eq 'PT1M') $true `
        'the trigger repeats every minute'
    Assert-SelfTestEqual (
        [string]$task.Triggers[0].StartBoundary.Length -gt 0) $true `
        'the trigger has a start boundary'
    Assert-SelfTestEqual (
        [string]$task.Settings.MultipleInstances -eq 'IgnoreNew') $true `
        'the task ignores a new instance'
    Assert-SelfTestEqual (
        [string]$task.Settings.ExecutionTimeLimit -eq 'PT5M') $true `
        'the run is bounded to five minutes'
    Assert-SelfTestEqual ([bool]$task.Settings.StartWhenAvailable) $true `
        'a missed run starts when available'
    Assert-SelfTestEqual ([bool]$task.Settings.DisallowStartIfOnBatteries) $false `
        'battery start is allowed'
    Assert-SelfTestEqual ([bool]$task.Settings.StopIfGoingOnBatteries) $false `
        'a battery transition does not stop the task'
    $principal = $task.Principal
    Assert-SelfTestEqual ([string]$principal.LogonType) 'Interactive' `
        'the principal logs on interactively'
    Assert-SelfTestEqual ([string]$principal.RunLevel) 'Limited' `
        'the principal runs limited (never SYSTEM)'
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Assert-SelfTestEqual ([string]$principal.UserId) $currentIdentity `
        'the principal is the current operator identity'
    Assert-SelfTestEqual ([string]$principal.UserId -eq 'SYSTEM') $false `
        'the principal is never SYSTEM'
    if ($script:FailureCount -gt 0) {
        throw "$($script:FailureCount) self-test assertion(s) failed"
    }
    Write-Host 'register-share-backend-watch self-test: PASS'
    return 0
}

if ($SelfTest) {
    exit (Invoke-RegisterSelfTest)
}

if ($Action -eq 'Unregister') {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output "Scheduled task not present: $TaskName"
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Unregistered scheduled task: $TaskName"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $script:WatchScript)) {
    throw "watch script not found: $($script:WatchScript)"
}
if (-not (Test-Path -LiteralPath $script:HiddenLauncher)) {
    # Fail closed with the exact fixed path instead of registering a task
    # that could never run.
    throw "hidden launcher not found: $($script:HiddenLauncher)"
}
if (-not (Test-Path -LiteralPath $script:WscriptExe)) {
    throw 'wscript.exe was not found'
}

$task = New-ShareBackendWatchTask
if ($PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output "Registered scheduled task: $TaskName"
}
exit 0
