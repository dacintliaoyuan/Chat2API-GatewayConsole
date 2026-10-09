[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Register', 'Unregister')]
    [string]$Action = 'Register',
    [string]$TaskName = 'AI Gateway Public Chat2API Watch'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Rev13-W (2026-10-04): the watch lifecycle is part of the private instance's
# opening intent (maintainer-approved option A). Registering also enables the
# lifecycle control marker - the deployment-window opt-in that lets
# gateway-runtime arm/release this task as chat2api is started/stopped;
# unregistering removes it. Before that marker exists the shipped runtime
# never touches the real scheduled task. Real registration stays a deployment
# action; tests substitute the ScheduledTask cmdlets and never run this file
# against the real scheduler.

$runtimeLibrary = Join-Path $PSScriptRoot 'gateway-runtime.ps1'
if (-not (Test-Path -LiteralPath $runtimeLibrary)) {
    throw "runtime library not found: $runtimeLibrary"
}
. $runtimeLibrary

$watchScript = Join-Path $PSScriptRoot 'watch-chat2api.ps1'
if (-not (Test-Path -LiteralPath $watchScript)) {
    throw "watch script not found: $watchScript"
}

# The runtime arms/releases EXACTLY the descriptor's task name; a renamed
# task would silently detach the watch from the lifecycle.
$descriptorTaskName = Get-GatewayWatchTaskName -Key 'chat2api'
if (-not $descriptorTaskName -or $descriptorTaskName -cne $TaskName) {
    throw ('watch task name mismatch: descriptor "{0}", parameter "{1}"' -f
        $descriptorTaskName, $TaskName)
}

if ($Action -eq 'Unregister') {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output "Scheduled task not present: $TaskName"
    }
    elseif ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Unregistered scheduled task: $TaskName"
    }
    # Unregistering the watch deployment also withdraws the lifecycle opt-in:
    # the runtime must stop driving the (now absent) task.
    if ($PSCmdlet.ShouldProcess((Get-GatewayWatchControlPath),
            'Remove watch lifecycle control marker')) {
        [void](Disable-GatewayWatchLifecycleControl)
        Write-Output 'Watch lifecycle control disabled'
    }
    exit 0
}

# Round 19 (2026-09-30): the action goes through wscript and a hidden VBS
# launcher. powershell.exe -WindowStyle Hidden still flashes a console window
# each run, which a once-a-minute task must not do; the VBS waits for the watch
# and returns its exit code as the task result.
$hiddenLauncher = Join-Path $PSScriptRoot 'watch-chat2api-hidden.vbs'
if (-not (Test-Path -LiteralPath $hiddenLauncher)) {
    throw "hidden launcher not found: $hiddenLauncher"
}
$wscriptExe = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $wscriptExe)) {
    throw 'wscript.exe was not found'
}

$arguments = '//B //Nologo "{0}"' -f $hiddenLauncher
$taskAction = New-ScheduledTaskAction -Execute $wscriptExe -Argument $arguments

# One persistent trigger with a one-minute repetition interval. The task
# survives reboots; StartWhenAvailable catches a missed repetition after login.
# Rev13-W: registration itself arms nothing - on the first run the watch
# checks this boot's intent and start outcome and releases its own scheduling
# when nothing authorizes recovery (e.g. a fresh machine, or a reboot after a
# previous boot's stop). The runtime re-arms it when chat2api is started.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity `
    -LogonType Interactive -RunLevel Limited
# A run that starts chat2api can take the 20s start timeout plus the stop
# check; a one-minute limit could end the task mid-start. The limit only
# bounds the watch run itself: a chat2api it started keeps running after the
# task ends (measured 2026-09-30 with a same-launcher dummy: alive 24 minutes
# after its task completed).
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $taskAction -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description ('Private chat2api (8000) watch, every 60s while armed: restarts only a vanished ' +
        'private instance whose operator intent is "running" for this boot AND whose start was ' +
        'health-confirmed (start_outcome=succeeded), after two failed probes; never after a ' +
        'console/CLI stop, never for 401/5xx; at most 3 starts per hour. The runtime arms this ' +
        'task when chat2api is started and releases it on a deliberate stop; the watch disarms ' +
        'itself when this boot authorizes no recovery. ' +
        'Log: %LOCALAPPDATA%\AIGatewayPublic\watch\chat2api-watch.log')

if ($PSCmdlet.ShouldProcess($TaskName,
        'Register scheduled task and enable watch lifecycle control')) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    [void](Enable-GatewayWatchLifecycleControl)
    Write-Output "Registered scheduled task: $TaskName"
    Write-Output ('Watch lifecycle control enabled: {0}' -f (Get-GatewayWatchControlPath))
}
exit 0
