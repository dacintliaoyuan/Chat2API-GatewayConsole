[CmdletBinding(DefaultParameterSetName = 'Launch')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Attach')][int]$AttachProcessId,
    [Parameter(Mandatory = $true, ParameterSetName = 'Attach')][long]$StartedUtcTicks,
    [Parameter(Mandatory = $true, ParameterSetName = 'Attach')][string]$ExpectedExecutable,
    [Parameter(Mandatory = $true, ParameterSetName = 'Attach')][string]$RecordDirectory
)

# Observe one local gateway process. Scheduling and restart decisions stay with
# the existing watch task. No stderr text, arguments or environment go to disk.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Save-ExitRecord {
    param([string]$Directory, [string]$Name, $Record)
    [void][IO.Directory]::CreateDirectory($Directory)
    $path = Join-Path $Directory ('exit-' + $Name + '.json')
    $pending = $path + '.' + $PID + '.tmp'
    try {
        [IO.File]::WriteAllText($pending, ($Record | ConvertTo-Json -Depth 4 -Compress),
            (New-Object Text.UTF8Encoding($false)))
        [IO.File]::Move($pending, $path)
    } finally {
        if (Test-Path -LiteralPath $pending -PathType Leaf) {
            Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue
        }
    }
    Get-ChildItem -LiteralPath $Directory -File -Filter 'exit-*.json' |
        Where-Object Name -Match '^exit-[0-9]+-[0-9]+\.json$' |
        Sort-Object LastWriteTimeUtc, Name -Descending |
        Select-Object -Skip 32 |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
}

function Add-ErrorCategory {
    param([string]$Line, $Counts)
    if ([string]::IsNullOrWhiteSpace($Line)) { return }
    $category = switch -Regex ($Line) {
        '^thread .* panicked at' { 'rust_panic'; break }
        '^um-gateway-candidate:.*(bind|address already in use|10048)' { 'listener_bind'; break }
        '^um-gateway-candidate:' { 'startup_error'; break }
        '^um-gateway: login (browser|devtools)' { 'login_browser'; break }
        default { 'other' }
    }
    if ($Counts[$category] -lt 2147483647) { $Counts[$category] += 1 }
}

function Read-ErrorSummary {
    param([Diagnostics.Process]$Process)
    $counts = [ordered]@{
        rust_panic = 0; listener_bind = 0; startup_error = 0; login_browser = 0; other = 0
    }
    $buffer = New-Object 'char[]' 4096
    $line = New-Object Text.StringBuilder
    $reader = $Process.StandardError
    $pending = $reader.ReadAsync($buffer, 0, $buffer.Length)
    $drainLimit = $null
    $state = 'drain_timed_out'
    while ($true) {
        if ($Process.HasExited -and $null -eq $drainLimit) {
            $drainLimit = [Diagnostics.Stopwatch]::StartNew()
        }
        if ($null -ne $drainLimit -and $drainLimit.ElapsedMilliseconds -ge 1000) { break }
        if (!$pending.Wait(200)) { continue }
        $length = $pending.GetAwaiter().GetResult()
        if ($length -eq 0) {
            $state = 'available'
            break
        }
        for ($index = 0; $index -lt $length; $index++) {
            $ch = $buffer[$index]
            if ($ch -eq [char]10) {
                Add-ErrorCategory -Line $line.ToString() -Counts $counts
                [void]$line.Clear()
            } elseif ($line.Length -lt 512 -and $ch -ne [char]13) {
                [void]$line.Append($ch)
            }
        }
        $pending = $reader.ReadAsync($buffer, 0, $buffer.Length)
    }
    Add-ErrorCategory -Line $line.ToString() -Counts $counts
    return @{ state = $state; counts = $counts }
}

$process = $null
try {
    if ($PSCmdlet.ParameterSetName -eq 'Attach') {
        $observation = 'attached'
        $process = Get-Process -Id $AttachProcessId -ErrorAction Stop
        # Open and retain a process handle before checking PID identity.
        [void]$process.Handle
        if ($process.StartTime.ToUniversalTime().Ticks -ne $StartedUtcTicks -or
            ![string]::Equals($process.Path, $ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'existing gateway process identity changed'
        }
        $stderr = $null
        $stderrState = 'not_attached_at_start'
    } else {
        $observation = 'launched'
        $envName = 'SELFUSEAI_CHAT2API_OBSERVER_LAUNCH'
        $definition = [Environment]::GetEnvironmentVariable($envName, 'Process')
        [Environment]::SetEnvironmentVariable($envName, $null, 'Process')
        if (!$definition) { throw 'missing local gateway launch definition' }
        $launch = $definition | ConvertFrom-Json
        $RecordDirectory = [string]$launch.record_directory
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = [string]$launch.executable
        $info.Arguments = [string]$launch.arguments
        $info.WorkingDirectory = [string]$launch.working_directory
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardError = $true
        try {
            $process = [Diagnostics.Process]::Start($info)
        } catch {
            $now = [DateTime]::UtcNow
            Save-ExitRecord -Directory $RecordDirectory -Name ('{0}-{1}' -f $now.Ticks, $PID) -Record @{
                schema = 1; observation = $observation; state = 'launch_failed'
                at_utc = $now.ToString('o'); error_type = $_.Exception.GetType().Name
            }
            exit 1
        }
        $stderr = $null
        $stderrState = 'available'
    }
    $started = $process.StartTime.ToUniversalTime()
    $gatewayPid = $process.Id
    if ($observation -eq 'launched') {
        try {
            $summary = Read-ErrorSummary -Process $process
            $stderr = $summary.counts
            $stderrState = $summary.state
        }
        catch { $stderrState = 'read_failed' }
    }
    $process.WaitForExit()
    $exited = $process.ExitTime.ToUniversalTime()
    $exitCode = $process.ExitCode
    Save-ExitRecord -Directory $RecordDirectory -Name ('{0}-{1}' -f $started.Ticks, $gatewayPid) -Record @{
        schema = 1; observation = $observation; state = 'exited'; pid = $gatewayPid
        started_utc = $started.ToString('o'); exited_utc = $exited.ToString('o')
        exit_code = $exitCode; exit_code_hex = ('0x{0:X8}' -f $exitCode)
        stderr_state = $stderrState; stderr = $stderr
    }
} finally {
    if ($null -ne $process) { $process.Dispose() }
}
