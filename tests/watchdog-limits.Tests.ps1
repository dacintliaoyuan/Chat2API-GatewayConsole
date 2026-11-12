# Isolated helper responses; never calls WFP, firewall or scheduled tasks.
$ErrorActionPreference = 'Stop'
$scratch = Join-Path $env:TEMP ('share-limits-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
try {
    $deployPath = Join-Path $PSScriptRoot '..\um-share-deploy\deploy-elevated.ps1'
    $deployAst = [Management.Automation.Language.Parser]::ParseFile($deployPath, [ref]$null, [ref]$null)
    $selector = $deployAst.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Select-ShareWatchdogProcesses' }, $false)
    if (-not $selector) { throw 'Watchdog migration selector is missing' }
    . ([scriptblock]::Create($selector.Extent.Text))
    $watchdogFixture = 'C:\share fixture\watchdog-umshare.ps1'
    $processes = @(
        @{Name='powershell.exe';ProcessId=1;CommandLine=('powershell.exe -File "'+$watchdogFixture+'"')},
        @{Name='pwsh.exe';ProcessId=2;CommandLine=('pwsh.exe -File "'+$watchdogFixture+'"')},
        @{Name='pwsh.exe';ProcessId=99;CommandLine=('pwsh.exe -File "'+$watchdogFixture+'"')},
        @{Name='pwsh.exe';ProcessId=3;CommandLine='pwsh.exe -File "C:\other\watchdog-umshare.ps1"'},
        @{Name='pwsh.exe';ProcessId=4;CommandLine=('pwsh.exe -File "'+$watchdogFixture+'.backup"')}
    )
    $selected = @(Select-ShareWatchdogProcesses -Processes $processes -WatchdogPath $watchdogFixture -CurrentProcessId 99)
    if (($selected.ProcessId -join ',') -ne '1,2') { throw 'Only the owned PS5/PS7 watchdogs may be selected' }
    # Replace only the elevation boundary; exercise the formal wrapper with
    # an old scheduler PATH without starting a task or requesting elevation.
    $wrapper = Join-Path $PSScriptRoot '..\um-share-deploy\run-deploy-guarded.ps1'
    $wrapperResult = Join-Path $scratch 'wrapper.json'
    $wrapperFixture = Join-Path $scratch 'wrapper fixture.ps1'
    $fixtureText = @'
function Start-Process {
    param($FilePath,$Verb,[switch]$Wait,[switch]$PassThru,$WindowStyle,$ArgumentList)
    @{host=$FilePath;verb=$Verb;window=$WindowStyle;arguments=$ArgumentList} |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath '__RESULT__' -Encoding UTF8
    return [pscustomobject]@{ExitCode=0}
}
. '__WRAPPER__' -Stage Watchdog -Deploy 'C:\fixture\not-executed.ps1'
'@
    $fixtureText.Replace('__RESULT__',$wrapperResult.Replace("'","''")).Replace('__WRAPPER__',$wrapper.Replace("'","''")) |
        Set-Content -LiteralPath $wrapperFixture -Encoding UTF8
    $savedPath = $env:PATH
    try {
        $env:PATH = Join-Path $env:SystemRoot 'System32'
        & (Join-Path $PSHOME 'pwsh.exe') -NoProfile -NonInteractive -File $wrapperFixture | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Formal wrapper fixture failed' }
    } finally { $env:PATH = $savedPath }
    $wrapped = Get-Content -LiteralPath $wrapperResult -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not [IO.Path]::IsPathRooted($wrapped.host) -or (Split-Path $wrapped.host -Leaf) -ne 'pwsh.exe' -or
        $wrapped.verb -ne 'RunAs' -or $wrapped.window -ne 'Hidden') { throw 'Wrapper must select an absolute PS7 host with a hidden helper window' }
    Set-Content -LiteralPath (Join-Path $scratch 'backend-port.txt') -Value '8110' -Encoding ASCII
    # Task Scheduler can retain a PATH from before PowerShell 7 was installed.
    # The helper is harmless: this exercises the actual child-launch boundary.
    $originalPath = $env:PATH
    try {
        $env:PATH = Join-Path $env:SystemRoot 'System32'
        $fixtureHelper = Join-Path $scratch 'wfp fixture.ps1'
        Set-Content -LiteralPath $fixtureHelper -Value "Write-Output 'STRONG=OK'; exit 0" -Encoding ASCII
        . (Join-Path $PSScriptRoot '..\um-share-deploy\watchdog-umshare.ps1') -FunctionsOnly -RunRoot $scratch -WfpHelper $fixtureHelper
        $ErrorActionPreference = 'Stop'
        $coldPath = Get-LimitsState
        if ($coldPath[0] -ne $true -or $coldPath[1] -ne 'ok') { throw 'PowerShell helper must run with a scheduler PATH lacking pwsh' }
    } finally { $env:PATH = $originalPath }
    . (Join-Path $PSScriptRoot '..\um-share-deploy\watchdog-umshare.ps1') -FunctionsOnly -RunRoot $scratch
    $ErrorActionPreference = 'Stop'
    function pwsh { $global:LASTEXITCODE = $script:fixtureExit; Write-Output $script:fixtureOutput }
    $cases = @(
        @(0, 'STRONG=OK', $true, 'ok'),
        @(1, 'filter0=MISSING(FWP_E_FILTER_NOT_FOUND 0x80320003); STRONG=FAIL', $false, 'wfp-filter-missing'),
        @(1, 'filter0=[action:BAD]; STRONG=FAIL', $false, 'wfp-filter-mismatch'),
        @(1, 'filter0=UNREADABLE(ACCESS_DENIED 0x5); STRONG=FAIL', $false, 'detection-failure(wfp-query-failed)'),
        @(2, 'compile failed', $false, 'detection-failure(rc=2)')
    )
    foreach ($case in $cases) {
        $script:fixtureExit = $case[0]; $script:fixtureOutput = $case[1]
        $actual = Get-LimitsState
        if ($actual[0] -ne $case[2] -or $actual[1] -ne $case[3]) { throw ('unexpected verdict: ' + ($actual -join ', ')) }
    }
    Write-Output 'PASS: missing, mismatch, unreadable, helper error and success stay distinct'
    $script:rule = @{ Direction='Outbound'; Profile='Any'; Enabled=$true; Action='Block' }
    $script:port = @{ Protocol='Any'; LocalPort='Any'; RemotePort='Any' }
    $script:addr = @{ LocalAddress='Any'; RemoteAddress='Any' }
    $script:profileEnabled = $true
    function Get-NetFirewallRule { return [pscustomobject]$script:rule }
    function Get-NetFirewallPortFilter { return [pscustomobject]$script:port }
    function Get-NetFirewallAddressFilter { return [pscustomobject]$script:addr }
    function Get-NetFirewallApplicationFilter { return @{ Program=$AppExe } }
    function Get-NetFirewallProfile { foreach ($name in @('Domain','Private','Public')) { @{Name=$name;Enabled=$script:profileEnabled} } }
    if (-not (Get-FirewallState)[0]) { throw 'Unrestricted enabled fixture must pass' }
    foreach ($case in @(@('rule','Profile','Private'), @('port','Protocol','TCP'),
            @('port','LocalPort','1234'), @('port','RemotePort','443'),
            @('addr','LocalAddress','192.0.2.1'), @('addr','RemoteAddress','192.0.2.2'))) {
        $target = Get-Variable -Name $case[0] -Scope Script -ValueOnly
        $previous = $target[$case[1]]; $target[$case[1]] = $case[2]
        if ((Get-FirewallState)[0]) { throw ('Narrow firewall scope passed: ' + ($case -join '/')) }
        $target[$case[1]] = $previous
    }
    $script:profileEnabled = $false
    if ((Get-FirewallState)[1] -ne 'firewall-disabled') { throw 'Disabled firewall profile must fail' }
    function Get-NetFirewallProfile { throw 'fixture query failure' }
    if ((Get-FirewallState)[1] -notlike 'firewall-error:*') { throw 'Query failure must remain unavailable' }
    Write-Output 'PASS: narrowed firewall scopes and disabled profiles fail; query errors stay unavailable'
} finally {
    $resolved=[IO.Path]::GetFullPath($scratch)
    if(-not $resolved.StartsWith([IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')+'\') -or
        (Split-Path $resolved -Leaf) -notmatch '^share-limits-[a-f0-9]{32}$') { throw 'Unsafe fixture cleanup target' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
