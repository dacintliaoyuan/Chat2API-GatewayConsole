$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$observer = Join-Path $root 'lib/observe-chat2api.ps1'
$shellPath = (Get-Process -Id $PID).Path
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixture = Join-Path $tempRoot ('chat2api-exit-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
$childScript = Join-Path $fixture 'fake gateway.ps1'
$fakeMarker = 'fake-test-credential-that-must-never-be-written'
. (Join-Path $root 'lib/gateway-runtime.ps1')
$envName = 'SELFUSEAI_GATEWAY_WATCH_DIR'
$previous = [Environment]::GetEnvironmentVariable($envName, 'Process')
$previousSource = $env:SELFUSEAI_GATEWAY_ACTION_SOURCE
try {
    @"
[Console]::Error.WriteLine('thread fixture panicked at test.rs:1:')
[Console]::Error.WriteLine('Bearer $fakeMarker')
[Console]::Error.WriteLine([string]::new('x', 65536))
exit 23
"@ | Set-Content -LiteralPath $childScript -Encoding UTF8
    [Environment]::SetEnvironmentVariable($envName, $fixture, 'Process')
    $records = Join-Path $fixture 'chat2api-exits'
    [void][IO.Directory]::CreateDirectory($records)
    foreach ($n in 1..32) {
        $old = Join-Path $records ('exit-1-{0}.json' -f $n)
        [IO.File]::WriteAllText($old, '{}')
        [IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow.AddDays(-1))
    }
    Start-GatewayObservedProcess -Executable $shellPath `
        -Arguments @('-NoProfile', '-NonInteractive', '-File', ('"' + $childScript + '"')) `
        -WorkingDirectory $fixture -Component 'worker'
    $wait = [Diagnostics.Stopwatch]::StartNew()
    do {
        $files = @(Get-ChildItem -LiteralPath $records -Filter 'exit-*.json' -File)
        $new = @($files | Where-Object Name -Match '^exit-[0-9]{15,}-')
        if ($new.Count -eq 1 -and $files.Count -eq 32) { break }
        Start-Sleep -Milliseconds 100
    } while ($wait.Elapsed.TotalSeconds -lt 10)
    if ($new.Count -ne 1 -or $files.Count -ne 32) {
        throw 'expected one observed process exit and a 32-record retention limit'
    }
    $raw = Get-Content -LiteralPath $new[0].FullName -Raw
    $exitRecord = $raw | ConvertFrom-Json
    if ($exitRecord.exit_code -ne 23 -or $exitRecord.pid -le 0 -or
        !$exitRecord.started_utc -or !$exitRecord.exited_utc -or
        $exitRecord.stderr_state -ne 'available' -or
        $exitRecord.stderr.rust_panic -ne 1 -or $exitRecord.stderr.other -ne 2 -or
        $exitRecord.component -ne 'worker' -or $null -ne $exitRecord.stop_request) {
        throw 'incorrect exit code, lifecycle, or stderr summary'
    }
    if ($raw.Contains($fakeMarker) -or $raw.Contains('Bearer') -or $raw.Length -gt 4096) {
        throw 'raw diagnostic text escaped the bounded summary'
    }

    $attachedRecordDir = Join-Path $fixture 'attached'
    $env:SELFUSEAI_GATEWAY_ACTION_SOURCE = 'console'
    'Start-Sleep -Seconds 3; exit 31' | Set-Content -LiteralPath $childScript -Encoding UTF8
    $child = Start-Process -FilePath $shellPath -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-File', ('"' + $childScript + '"')) `
        -WindowStyle Hidden -PassThru
    try {
        $target = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
        $wrongTarget = [pscustomobject]@{ ProcessId = $child.Id; CreationDate = $target.CreationDate.AddSeconds(-1) }
        Write-GatewayStopRequest -Target $wrongTarget -Method 'force_stop' -RecordDirectory $attachedRecordDir
        if (Test-Path -LiteralPath $attachedRecordDir) { throw 'stop request was recorded for a reused PID' }
        Write-GatewayStopRequest -Target $target -Method 'graceful_http' -RecordDirectory $attachedRecordDir
        & $shellPath -NoProfile -NonInteractive -File $observer `
            -AttachProcessId $child.Id -StartedUtcTicks ($child.StartTime.ToUniversalTime().Ticks) `
            -ExpectedExecutable $shellPath -RecordDirectory $attachedRecordDir -Component 'ingress'
        if ($LASTEXITCODE -ne 0) { throw 'attached observer failed' }
        $file = Get-ChildItem -LiteralPath $attachedRecordDir -Filter 'exit-*.json' -File | Select-Object -First 1
        $attached = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
        if ($attached.exit_code -ne 31 -or $attached.pid -ne $child.Id -or
            $attached.observation -ne 'attached' -or $null -ne $attached.stderr -or
            $attached.component -ne 'ingress' -or $attached.stop_request.source -ne 'console' -or
            $attached.stop_request.method -ne 'graceful_http') {
            throw 'invalid attached process exit evidence'
        }
    } finally {
        if (!$child.HasExited) { $child.Kill(); $child.WaitForExit() }
        $child.Dispose()
    }
    Write-Host 'chat2api exit observer self-test: PASS'
} finally {
    [Environment]::SetEnvironmentVariable($envName, $previous, 'Process')
    [Environment]::SetEnvironmentVariable('SELFUSEAI_GATEWAY_ACTION_SOURCE', $previousSource, 'Process')
    $resolved = (Resolve-Path -LiteralPath $fixture).Path
    if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved) -match '^chat2api-exit-test-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
