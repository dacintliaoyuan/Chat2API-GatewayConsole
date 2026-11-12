# Protected, fixed-scope broker maintenance entry. Never opens public sharing.
param(
    [Parameter(Mandatory=$true)][string]$RunRoot,
    [ValidateRange(10,60)][int]$BudgetSeconds=45,
    [switch]$FunctionsOnly
)
$ErrorActionPreference = 'Stop'

function Invoke-LimitsCommand {
    param([string]$Exe, [string[]]$Arguments, [Diagnostics.Stopwatch]$Clock)
    $remaining = $BudgetSeconds - $Clock.Elapsed.TotalSeconds
    if ($remaining -le 0) { throw 'repair_timeout' }
    $base = Join-Path (Join-Path $RunRoot 'broker') ('repair-' + [guid]::NewGuid().ToString('N'))
    $outPath = $base + '.out'; $errPath = $base + '.err'
    $process = $null
    try {
        $quoted = @($Arguments | ForEach-Object {
            if ($_ -match '["\r\n]') { throw 'invalid_deployment' }
            '"' + $_ + '"'
        })
        $process = Start-Process -FilePath $Exe -ArgumentList ($quoted -join ' ') -WindowStyle Hidden `
            -RedirectStandardOutput $outPath -RedirectStandardError $errPath -PassThru
        # PowerShell 5.1 must retain the native handle before waiting; otherwise
        # a completed Start-Process child can expose a null ExitCode.
        $null = $process.Handle
        if (-not $process.WaitForExit([int]($remaining * 1000))) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            [void]$process.WaitForExit(1000)
            throw 'repair_timeout'
        }
        $process.Refresh()
        return @{ Code=$process.ExitCode; Text=(Get-Content -LiteralPath $outPath -Raw -Encoding UTF8) }
    } finally {
        foreach ($path in @($outPath,$errPath)) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        if ($process) { $process.Dispose() }
    }
}

function Invoke-ShareLimitsRepair {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    # Do not trust a user-writable copy, a substituted root or a junction.
    if ([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -ne [IO.Path]::GetFullPath($RunRoot).TrimEnd('\')) { throw 'invalid_deployment' }
    foreach ($path in @($RunRoot, (Join-Path $RunRoot 'wfp_helper.ps1'), (Join-Path $RunRoot 'watchdog-umshare.ps1'))) {
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'invalid_deployment' }
    }
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8100 })
    if ($listeners.Count -ne 0) { throw 'gateway_not_closed' }
    $lifecycle = Get-Content -LiteralPath (Join-Path $RunRoot 'broker\lifecycle.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lifecycle.state -ne 'stopped') { throw 'stop_intent_unconfirmed' }
    $tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    $public = Invoke-LimitsCommand -Exe $tailscale -Arguments @('funnel','status','--json') -Clock $clock
    if ($public.Code -ne 0) { throw 'public_unconfirmed' }
    $mapping = $public.Text | ConvertFrom-Json
    if ($null -ne $mapping.TCP.'443' -or @($mapping.AllowFunnel.PSObject.Properties | Where-Object { $_.Value -eq $true }).Count -ne 0) { throw 'public_not_closed' }
    $portText = (Get-Content -LiteralPath (Join-Path $RunRoot 'backend-port.txt') -Raw -Encoding ASCII).Trim()
    $port = 0
    if (-not [int]::TryParse($portText,[ref]$port) -or $port -lt 1 -or $port -gt 65535) { throw 'invalid_deployment' }
    $helper = Join-Path $RunRoot 'wfp_helper.ps1'
    $image = Join-Path $RunRoot 'um-share-python.exe'
    $psExe = Join-Path $PSHOME 'pwsh.exe'
    $task = Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop
    $expectedArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $RunRoot 'watchdog-umshare.ps1') + '"'
    if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -notin @('pwsh.exe', (Join-Path $PSHOME 'pwsh.exe')) -or $task.Actions[0].Arguments -ne $expectedArgs -or
        $task.Principal.UserId -notin @('SYSTEM','NT AUTHORITY\SYSTEM','S-1-5-18') -or
        @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -lt 1) { throw 'watchdog_not_boot_ready' }
    # Public and gateway are already closed. Pause only the verified watcher:
    # its limit-loss cleanup must not delete the password-backed live task
    # during the intentional remove/add window. Always re-arm on failure too.
    try {
        if ($task.State -eq 'Running') {
            Stop-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop
            while ((Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop).State -eq 'Running') {
                if ($clock.Elapsed.TotalSeconds -ge $BudgetSeconds) { throw 'repair_timeout' }
                Start-Sleep -Milliseconds 200
            }
        }
        foreach ($action in @('remove','add','strong')) {
            $check = Invoke-LimitsCommand -Exe $psExe -Arguments @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                '-File',$helper,'-Action',$action,'-AppExePath',$image,'-Port',[string]$port) -Clock $clock
            if ($check.Code -ne 0) { throw 'wfp_repair_failed' }
            if ($action -eq 'strong' -and $check.Text -notmatch 'STRONG=OK') { throw 'wfp_repair_failed' }
        }
        $ruleName = 'um-share-outbound-block'
        # Create only the dedicated image's missing rule. Existing narrowed rules
        # or disabled global profiles need explicit administrator diagnosis.
        $rule = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.DisplayName -eq $ruleName })
        if ($rule.Count -eq 0) {
            New-NetFirewallRule -DisplayName $ruleName -Direction Outbound -Action Block -Program $image -Profile Any -ErrorAction Stop | Out-Null
        }
        . (Join-Path $RunRoot 'watchdog-umshare.ps1') -FunctionsOnly -RunRoot $RunRoot
        $fw = Get-FirewallState
        if ($fw[0] -ne $true) { throw 'firewall_not_satisfied' }
    } finally {
        Start-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop
    }
    do {
        if ((Get-ScheduledTask -TaskName 'umshare-watchdog' -ErrorAction Stop).State -eq 'Running') { break }
        if ($clock.Elapsed.TotalSeconds -ge $BudgetSeconds) { throw 'repair_timeout' }
        Start-Sleep -Milliseconds 200
    } while ($true)
    if (@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8100 }).Count -ne 0) { throw 'gateway_not_closed' }
    return @{repaired=$true;code='ok'}
}

if ($FunctionsOnly) { return }
try {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'administrator_required' }
    $result = Invoke-ShareLimitsRepair
    Write-Output ('REPAIR_RESULT=' + ($result | ConvertTo-Json -Compress))
    exit 0
} catch {
    $known = @('administrator_required','invalid_deployment','gateway_not_closed','stop_intent_unconfirmed',
        'public_unconfirmed','public_not_closed','repair_timeout','wfp_repair_failed','firewall_not_satisfied','watchdog_not_boot_ready')
    $code = if ($_.Exception.Message -in $known) { $_.Exception.Message } else { 'repair_failed' }
    Write-Output ('REPAIR_RESULT=' + (@{repaired=$false;code=$code} | ConvertTo-Json -Compress))
    exit 1
}
