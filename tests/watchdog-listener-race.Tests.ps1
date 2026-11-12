# Offline regression for a listener disappearing during watchdog identity reads.
# Every OS query is replaced; the production watchdog decision function is real.
$ErrorActionPreference = 'Stop'
$scratch = Join-Path $env:TEMP ('watchdog-listener-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($scratch)
try {
    . (Join-Path $PSScriptRoot '..\um-share-deploy\watchdog-umshare.ps1') `
        -FunctionsOnly -RunRoot $scratch -WatchdogStateDir $scratch
    $ErrorActionPreference = 'Stop'
    $script:scenario = ''
    $script:listenerReads = 0
    function Get-NetTCPConnection {
        [CmdletBinding()]
        param([int]$LocalPort, [string]$State)
        $script:listenerReads++
        if ($script:scenario -eq 'absent') { return }
        if ($script:scenario -eq 'not-found') {
            $record = [Management.Automation.ErrorRecord]::new(
                [InvalidOperationException]::new('No matching listener'),
                'CmdletizationQuery_NotFound',
                [Management.Automation.ErrorCategory]::ObjectNotFound, $LocalPort)
            $PSCmdlet.ThrowTerminatingError($record)
        }
        if ($script:scenario -eq 'initial-query-error' -or
            ($script:scenario -eq 'recheck-error' -and $script:listenerReads -gt 1)) {
            Write-Error 'fixture listener query unavailable'
            return
        }
        if ($script:scenario -in @('process-gone', 'owner-read-after-exit') -and
            $script:listenerReads -gt 1) { return }
        $listenerPid = if ($script:scenario -eq 'replacement' -and
            $script:listenerReads -gt 1) { 8181 } else { 4242 }
        return [pscustomobject]@{ OwningProcess = $listenerPid }
    }
    function Get-CimInstance {
        [CmdletBinding()]
        param([string]$ClassName, [string]$Filter)
        if ($script:scenario -in @('process-gone', 'replacement', 'still-present', 'recheck-error')) { return }
        return [pscustomobject]@{ ExecutablePath = $ExpectedExe; CreationDate = [datetime]'2026-10-23T10:00:00Z' }
    }
    function Invoke-CimMethod {
        [CmdletBinding()]
        param($InputObject, [string]$MethodName)
        if ($null -eq $InputObject) { throw 'fixture cannot read owner of an exited process' }
        if ($script:scenario -in @('owner-read-after-exit', 'owner-query-error')) { throw 'fixture owner query unavailable' }
        $user = if ($script:scenario -eq 'wrong-owner') { 'foreign-owner' } else { 'um_share' }
        return [pscustomobject]@{ User = $user }
    }
    $cases = @(
        @('absent', 'absent'),
        @('not-found', 'absent'),
        @('verified', 'verified'),
        @('process-gone', 'absent'),
        @('owner-read-after-exit', 'absent'),
        @('replacement', 'unconfirmed'),
        @('still-present', 'unconfirmed'),
        @('recheck-error', 'unconfirmed'),
        @('initial-query-error', 'unconfirmed'),
        @('owner-query-error', 'unconfirmed'),
        @('wrong-owner', 'unconfirmed')
    )
    $failures = 0
    foreach ($case in $cases) {
        $script:scenario = $case[0]
        $script:listenerReads = 0
        $actual = Get-ShareState
        if ($actual.state -ne $case[1]) {
            Write-Host ('FAIL {0}: expected {1}, got {2} ({3})' -f $case[0], $case[1], $actual.state, $actual.err)
            $failures++
        } else { Write-Host ('PASS ' + $case[0]) }
        if ($script:listenerReads -gt 2) { throw 'Listener recheck must be bounded to one extra query' }
    }
    if ($failures -gt 0) { throw ('watchdog listener race: {0} failures' -f $failures) }
    Write-Output 'PASS: exited listeners, replacement listeners and unavailable queries stay distinct'
} finally {
    $resolved = [IO.Path]::GetFullPath($scratch)
    $tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected scratch path' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
