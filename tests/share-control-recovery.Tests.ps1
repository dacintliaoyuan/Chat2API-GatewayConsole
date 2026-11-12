# Offline tests: all scheduler, console and elevation boundaries are replaced.
# No test invokes UAC, production HTTP, tasks or gateway processes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$root = Split-Path -Parent $PSScriptRoot
$library = Join-Path $root 'lib\share-control-recovery.ps1'
if (-not (Test-Path -LiteralPath $library)) {
    Write-Host 'FAIL fixed share-control recovery entry is not implemented' -ForegroundColor Red
    exit 1
}
. $library
$script:failures = 0
$script:checks = 0
function Assert-Equal {
    param($Actual, $Expected, [string]$Name)
    $script:checks++
    if ([object]::Equals($Actual, $Expected)) { Write-Host "PASS $Name"; return }
    $script:failures++
    Write-Host "FAIL $Name : expected '$Expected', got '$Actual'" -ForegroundColor Red
}
function Assert-Code {
    param([scriptblock]$Block, [string]$Code, [string]$Name)
    $actual = 'did_not_throw'
    try { & $Block | Out-Null } catch { $actual = Get-ShareControlErrorCode $_.Exception }
    Assert-Equal $actual $Code $Name
}

function New-TaskXml {
    param([string]$TaskName = 'umshare-broker')
    $command = 'C:\ProgramData\UMShareRun\run-broker.cmd'
    $arguments = ''
    $logon = 'S4U'
    if ($TaskName -eq 'umshare-watchdog') {
        $command = Join-Path $PSHOME 'pwsh.exe'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "C:\ProgramData\UMShareRun\watchdog-umshare.ps1"'
        $logon = 'ServiceAccount'
    }
    return @"
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><LogonType>$logon</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><Enabled>true</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><AllowStartOnDemand>true</AllowStartOnDemand></Settings>
  <Actions Context="Author"><Exec><Command>$command</Command><Arguments>$arguments</Arguments><WorkingDirectory>C:\ProgramData\UMShareRun</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function New-TaskRuntimeFacts {
    param([string]$TaskName = 'umshare-broker')
    return @{ principal_logon_type = $(if ($TaskName -eq 'umshare-broker') { 2 } else { 5 });
        enabled = $true; allow_demand_start = $true }
}

function New-Fixture {
    param([string]$BrokerState = 'Running', [string]$WatchdogState = 'Running', [bool]$Healthy = $false,
        [bool]$ConsoleRefreshSufficient = $false)
    $state = @{
        Tasks = @{}; Events = [System.Collections.Generic.List[string]]::new()
        Healthy = $Healthy; Denied = ''; Missing = ''; Foreign = ''
        StopStuck = $false; StartStuck = ''; MutateOnStop = $false
        ReloadOk = $true; HttpThrows = $false; HttpUnknown = $false
        Locked = $false; Released = 0; Tick = 0L; BrokerRestarted = $false
        ConsoleRefreshSufficient = $ConsoleRefreshSufficient
        ProcessCount = $(if ($BrokerState -eq 'Ready') { 0 } else { 1 })
        ProcessQueryFails = $false; OrphanSurvives = $false
    }
    foreach ($name in @('umshare-broker', 'umshare-watchdog')) {
        $facts = ConvertFrom-ShareControlTaskXml -TaskName $name -TaskPath "\$name" -Xml (New-TaskXml $name) -State 'Running' -RuntimeFacts (New-TaskRuntimeFacts $name)
        $state.Tasks[$name] = $facts
    }
    $state.Tasks['umshare-broker'].state = $BrokerState
    $state.Tasks['umshare-watchdog'].state = $WatchdogState
    $adapter = @{
        ReadTask = {
            param($name)
            $state.Events.Add("read:$name")
            if ($state.Denied -eq $name) { throw 'task_access_denied' }
            if ($state.Missing -eq $name) { throw 'task_missing' }
            $copy = @{} + $state.Tasks[$name]
            if ($state.Foreign -eq $name) { $copy.owned = $false; $copy.code = 'task_identity_mismatch' }
            return $copy
        }.GetNewClosure()
        StopTask = {
            param($name, $hash)
            $state.Events.Add("stop:$name")
            if ($state.Tasks[$name].definition_hash -ne $hash) { throw 'task_definition_changed' }
            if (-not $state.StopStuck) { $state.Tasks[$name].state = 'Ready' }
            if (-not $state.StopStuck -and -not $state.OrphanSurvives) { $state.ProcessCount = 0 }
            if ($state.MutateOnStop) { $state.Tasks[$name].definition_hash = ('b' * 64) }
        }.GetNewClosure()
        StartTask = {
            param($name, $hash)
            $state.Events.Add("start:$name")
            if ($state.Tasks[$name].definition_hash -ne $hash) { throw 'task_definition_changed' }
            if ($state.StartStuck -ne $name) {
                $state.Tasks[$name].state = 'Running'
                if ($name -eq 'umshare-broker') { $state.BrokerRestarted = $true; $state.ProcessCount = 1 }
            }
        }.GetNewClosure()
        ReadBrokerProcessCount = {
            $state.Events.Add('read:broker-process')
            if ($state.ProcessQueryFails) { throw 'broker_process_unconfirmed' }
            return $state.ProcessCount
        }.GetNewClosure()
        ReloadConsole = {
            $state.Events.Add('reload:usage')
            if (-not $state.ReloadOk) { throw 'console_reload_failed' }
            $state.Healthy = $state.ConsoleRefreshSufficient -or $state.BrokerRestarted
        }.GetNewClosure()
        ReadBroker = {
            $state.Events.Add('http:broker')
            if ($state.HttpThrows) { throw 'console_status_unavailable' }
            if ($state.HttpUnknown) { return $null }
            return $state.Healthy
        }.GetNewClosure()
        AcquireLock = {
            $state.Events.Add('lock')
            if ($state.Locked) { throw 'recovery_busy' }
            $state.Locked = $true
            return 'fixture-lock'
        }.GetNewClosure()
        ReleaseLock = {
            param($lock)
            $state.Events.Add('unlock')
            $state.Locked = $false
            $state.Released++
        }.GetNewClosure()
        Now = { return $state.Tick }.GetNewClosure()
        Delay = { param($milliseconds) $state.Tick += [long]$milliseconds }.GetNewClosure()
    }
    return @{ State = $state; Adapter = $adapter }
}

function Invoke-Fixture {
    param($Fixture, [string]$Action = 'Recover')
    return Invoke-ShareControlRecovery -Action $Action -Adapter $Fixture.Adapter
}

# Sanitized shape of the two actual Scheduler exports: default nodes are
# omitted even though COM reports SYSTEM ServiceAccount, enabled, demand-start.
foreach ($name in @('umshare-broker', 'umshare-watchdog')) {
    $exported = (New-TaskXml $name) -replace '<LogonType>[^<]+</LogonType>', ''
    $exported = $exported.Replace('<Enabled>true</Enabled>', '').Replace('<AllowStartOnDemand>true</AllowStartOnDemand>', '')
    $arguments = @{ TaskName = $name; TaskPath = "\$name"; Xml = $exported; State = 'Running' }
    $arguments.RuntimeFacts = @{ principal_logon_type = 5; enabled = $true; allow_demand_start = $true }
    $facts = ConvertFrom-ShareControlTaskXml @arguments
    Assert-Equal $facts.owned $true "$name Scheduler export with omitted defaults uses actual COM facts"
    $arguments.RuntimeFacts = $null
    $facts = ConvertFrom-ShareControlTaskXml @arguments
    Assert-Equal $facts.owned $false "$name omitted defaults never assume runtime facts"
    foreach ($field in @('principal_logon_type', 'enabled', 'allow_demand_start')) {
        $arguments.RuntimeFacts = @{ principal_logon_type = 5; enabled = $true; allow_demand_start = $true }
        $arguments.RuntimeFacts.Remove($field)
        $facts = ConvertFrom-ShareControlTaskXml @arguments
        Assert-Equal $facts.owned $false "$name requires actual $field when defaults are omitted"
    }
    foreach ($case in @(
        @{ Name = 'string logon type'; Facts = @{ principal_logon_type = '5'; enabled = $true; allow_demand_start = $true } },
        @{ Name = 'password logon'; Facts = @{ principal_logon_type = 1; enabled = $true; allow_demand_start = $true } },
        @{ Name = 'disabled runtime'; Facts = @{ principal_logon_type = 5; enabled = $false; allow_demand_start = $true } },
        @{ Name = 'demand start denied'; Facts = @{ principal_logon_type = 5; enabled = $true; allow_demand_start = $false } },
        @{ Name = 'string enabled'; Facts = @{ principal_logon_type = 5; enabled = 'true'; allow_demand_start = $true } },
        @{ Name = 'string demand start'; Facts = @{ principal_logon_type = 5; enabled = $true; allow_demand_start = 'true' } }
    )) {
        $arguments.RuntimeFacts = $case.Facts
        $facts = ConvertFrom-ShareControlTaskXml @arguments
        Assert-Equal $facts.owned $false "$name rejects $($case.Name) COM facts"
    }
}
function Get-Mutations {
    param($Fixture)
    return (@($Fixture.State.Events | Where-Object { $_ -match '^(stop|start|reload):' }) -join ',')
}

# Real task XML parsing, not just a fake owned flag.
foreach ($name in @('umshare-broker', 'umshare-watchdog')) {
    $facts = ConvertFrom-ShareControlTaskXml -TaskName $name -TaskPath "\$name" -Xml (New-TaskXml $name) -State 'Running' -RuntimeFacts (New-TaskRuntimeFacts $name)
    Assert-Equal $facts.owned $true "$name exact fixed identity is accepted"
    Assert-Equal $facts.state 'Running' "$name keeps observed task state"
    Assert-Equal $facts.definition_hash.Length 64 "$name binds mutation to the complete definition"
}
foreach ($case in @(
    @{ Name = 'wrong principal'; Xml = (New-TaskXml).Replace('S-1-5-18', 'S-1-5-19') },
    @{ Name = 'group principal'; Xml = (New-TaskXml).Replace('<UserId>S-1-5-18</UserId>', '<GroupId>S-1-5-18</GroupId>') },
    @{ Name = 'multiple principals'; Xml = (New-TaskXml).Replace('</Principals>', '<Principal id="Other"><UserId>S-1-5-18</UserId></Principal></Principals>') },
    @{ Name = 'extra action'; Xml = (New-TaskXml).Replace('</Actions>', '<ComHandler><ClassId>other</ClassId></ComHandler></Actions>') },
    @{ Name = 'wrong wrapper'; Xml = (New-TaskXml).Replace('run-broker.cmd', 'other.cmd') },
    @{ Name = 'wrapper arguments'; Xml = (New-TaskXml).Replace('<Arguments></Arguments>', '<Arguments>--other</Arguments>') },
    @{ Name = 'wrong working directory'; Xml = (New-TaskXml).Replace('<WorkingDirectory>C:\ProgramData\UMShareRun</WorkingDirectory>', '<WorkingDirectory>C:\other</WorkingDirectory>') },
    @{ Name = 'disabled'; Xml = (New-TaskXml).Replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>') },
    @{ Name = 'XML demand start denied'; Xml = (New-TaskXml).Replace('<AllowStartOnDemand>true</AllowStartOnDemand>', '<AllowStartOnDemand>false</AllowStartOnDemand>') },
    @{ Name = 'parallel instances'; Xml = (New-TaskXml).Replace('IgnoreNew', 'Parallel') },
    @{ Name = 'malformed'; Xml = '<Task>' },
    @{ Name = 'DTD'; Xml = '<!DOCTYPE Task [<!ENTITY other SYSTEM "file:///never-read">]>' + (New-TaskXml) },
    @{ Name = 'wrong watchdog arguments'; Task = 'umshare-watchdog'; Xml = (New-TaskXml 'umshare-watchdog').Replace('watchdog-umshare.ps1', 'other.ps1') },
    @{ Name = 'bare watchdog host'; Task = 'umshare-watchdog'; Xml = (New-TaskXml 'umshare-watchdog').Replace((Join-Path $PSHOME 'pwsh.exe'), 'pwsh.exe') }
)) {
    $name = if ($case.ContainsKey('Task')) { $case.Task } else { 'umshare-broker' }
    $facts = ConvertFrom-ShareControlTaskXml -TaskName $name -TaskPath "\$name" -Xml $case.Xml -State 'Running' -RuntimeFacts (New-TaskRuntimeFacts $name)
    Assert-Equal $facts.owned $false "$($case.Name) refuses task ownership"
}
$foreignPath = ConvertFrom-ShareControlTaskXml -TaskName 'umshare-broker' -TaskPath '\other\umshare-broker' -Xml (New-TaskXml) -State 'Running' -RuntimeFacts (New-TaskRuntimeFacts)
Assert-Equal $foreignPath.owned $false 'same task name in another folder refuses'

foreach ($case in @(
    @{ Name = 'explicit logon versus COM contradiction'; Facts = @{ principal_logon_type = 5; enabled = $true; allow_demand_start = $true } },
    @{ Name = 'explicit enabled versus COM contradiction'; Facts = @{ principal_logon_type = 2; enabled = $false; allow_demand_start = $true } },
    @{ Name = 'explicit demand start versus COM contradiction'; Facts = @{ principal_logon_type = 2; enabled = $true; allow_demand_start = $false } }
)) {
    $facts = ConvertFrom-ShareControlTaskXml -TaskName 'umshare-broker' -TaskPath '\umshare-broker' -Xml (New-TaskXml) -State 'Running' -RuntimeFacts $case.Facts
    Assert-Equal $facts.owned $false "$($case.Name) refuses"
}
$facts = ConvertFrom-ShareControlTaskXml -TaskName 'umshare-broker' -TaskPath '\umshare-broker' -Xml (New-TaskXml) -State 'Running'
Assert-Equal $facts.owned $false 'complete XML still requires independently obtained COM facts'

$access = [System.Runtime.InteropServices.COMException]::new('private data never echoed', -2147024891)
$wrapped = [System.Exception]::new('private outer data never echoed', $access)
Assert-Equal (Get-ShareControlErrorCode $wrapped) 'task_access_denied' 'wrapped E_ACCESSDENIED is not task missing'
$missing = [System.Runtime.InteropServices.COMException]::new('no localized text matching', -2147024894)
Assert-Equal (Get-ShareControlErrorCode $missing) 'task_missing' 'missing task requires a specific native error code'
Assert-Equal (Get-ShareControlErrorCode ([System.Exception]::new('a secret arbitrary failure'))) 'task_operation_failed' 'unknown exception text is not exposed'

$processRow = @{ ProcessId = 77; CreationDate = '20261019';
    ExecutablePath = 'C:\ProgramData\UMShareRun\um-share-python.exe';
    CommandLine = '"C:\ProgramData\UMShareRun\um-share-python.exe" "C:\ProgramData\UMShareRun\share_broker_server.py" --broker-dir "C:\ProgramData\UMShareRun\broker"' }
$systemOwner = { param($row) return @{ ReturnValue = 0; Sid = 'S-1-5-18' } }
$readProcess = { return $processRow }.GetNewClosure()
$processResult = Get-ShareControlBrokerProcessWorker -ProcessReader $readProcess -OwnerReader $systemOwner
Assert-Equal $processResult.count 1 'process worker verifies the full fixed image, script and SYSTEM owner'
$processResult = Get-ShareControlBrokerProcessWorker -ProcessReader { } -OwnerReader $systemOwner
Assert-Equal $processResult.count 0 'successful empty process inventory proves exit'
$processResult = Get-ShareControlBrokerProcessWorker -ProcessReader { throw 'private query failure' } -OwnerReader $systemOwner
Assert-Equal $processResult.ok $false 'failed process query never becomes empty inventory'
foreach ($case in @(
    @{ Name = 'wrong script path'; Field = 'CommandLine'; Value = '"C:\ProgramData\UMShareRun\um-share-python.exe" "C:\other\share_broker_server.py"' },
    @{ Name = 'missing command line'; Field = 'CommandLine'; Value = '' },
    @{ Name = 'missing creation'; Field = 'CreationDate'; Value = '' },
    @{ Name = 'invalid pid'; Field = 'ProcessId'; Value = 0 }
)) {
    $row = @{} + $processRow
    $row[$case.Field] = $case.Value
    $reader = { return $row }.GetNewClosure()
    $processResult = Get-ShareControlBrokerProcessWorker -ProcessReader $reader -OwnerReader $systemOwner
    Assert-Equal $processResult.ok $false "$($case.Name) cannot identify an owned broker"
}
$processResult = Get-ShareControlBrokerProcessWorker -ProcessReader $readProcess -OwnerReader { @{ ReturnValue = 0; Sid = 'S-1-5-19' } }
Assert-Equal $processResult.ok $false 'wrong process owner never becomes an owned broker'
$processResult = Get-ShareControlBrokerProcessWorker -ProcessReader $readProcess -OwnerReader { throw 'private owner query error' }
Assert-Equal $processResult.ok $false 'unknown process owner refuses recovery'

# Real native adapters run through their encoding/decoding and fixed argument
# construction, with only the last process/HTTP/elevation boundary substituted.
$leaf = @{ Arguments = @(); Timeout = 0; Url = ''; Host = ''; Verb = ''; Window = '' }
$fakeWorker = {
    param($argv, $seconds)
    $leaf.Arguments = $argv; $leaf.Timeout = $seconds
    return @{ ok = $true; facts = (ConvertFrom-ShareControlTaskXml -TaskName 'umshare-broker' -TaskPath '\umshare-broker' -Xml (New-TaskXml) -State 'Running' -RuntimeFacts (New-TaskRuntimeFacts)) } | ConvertTo-Json -Depth 5 -Compress
}.GetNewClosure()
$facts = Invoke-ShareControlTaskNative -Operation Read -TaskName 'umshare-broker' -ProcessRunner $fakeWorker
Assert-Equal $facts.owned $true 'native read parses the worker JSON receipt'
Assert-Equal $leaf.Timeout 6 'scheduler helper has a six-second deadline'
$workerBody = [System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($leaf.Arguments[-1]))
Assert-Equal ($workerBody -match "Invoke-ShareControlTaskWorker -Operation 'Read' -TaskName 'umshare-broker'") $true 'worker binds the exact task and operation'
$expectedHash = $facts.definition_hash
Invoke-ShareControlTaskNative -Operation Stop -TaskName 'umshare-broker' -ExpectedHash $expectedHash -ProcessRunner $fakeWorker
$workerBody = [System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($leaf.Arguments[-1]))
Assert-Equal ($workerBody.Contains("-ExpectedHash '$expectedHash'")) $true 'native mutation includes the inspected complete definition hash'
Assert-Code { Invoke-ShareControlTaskNative -Operation Read -TaskName 'umshare-broker' -ProcessRunner { '{"ok":false,"code":"task_access_denied"}' } } 'task_access_denied' 'native denied receipt remains denied'
Assert-Code { Invoke-ShareControlTaskNative -Operation Read -TaskName 'umshare-broker' -ProcessRunner { 'not-json' } } 'task_query_failed' 'native malformed receipt cannot pass'
Assert-Code { Invoke-ShareControlTaskNative -Operation Read -TaskName 'umshare-broker' -ProcessRunner { '{"ok":"true"}' } } 'task_query_failed' 'truthy worker strings cannot pass'
Assert-Equal (Read-ShareControlBrokerProcessCountNative -ProcessRunner { '{"ok":true,"count":0}' }) 0 'native process receipt can prove no matching broker'
Assert-Equal (Read-ShareControlBrokerProcessCountNative -ProcessRunner { '{"ok":true,"count":1}' }) 1 'native process receipt can identify one matching broker'
Assert-Code { Read-ShareControlBrokerProcessCountNative -ProcessRunner { '{"ok":false,"code":"private-data"}' } } 'broker_process_unconfirmed' 'failed process query cannot prove exit'
Assert-Code { Read-ShareControlBrokerProcessCountNative -ProcessRunner { '{"ok":true,"count":"0"}' } } 'broker_process_unconfirmed' 'process truthy strings cannot prove exit'

$httpReader = {
    param($url, $seconds)
    $leaf.Url = $url; $leaf.Timeout = $seconds
    return '{"responsive":true,"unrelated":"never printed"}'
}.GetNewClosure()
Assert-Equal (Read-ShareControlBrokerNative -HttpReader $httpReader) $true 'native HTTP adapter parses an affirmative boolean'
Assert-Equal $leaf.Url 'http://127.0.0.1:8001/api/console/share-management/control-status' 'HTTP adapter reads only the fixed credential-free control status'
Assert-Equal $leaf.Timeout 15 'HTTP request has one complete fifteen-second deadline'
Assert-Equal (Read-ShareControlBrokerNative -HttpReader { '{"responsive":false,"error_code":"deadline_exceeded"}' }) $false 'native HTTP adapter preserves a negative boolean'
Assert-Equal ($null -eq (Read-ShareControlBrokerNative -HttpReader { '{"responsive":"true"}' })) $true 'HTTP truthy strings remain unknown'
Assert-Equal ($null -eq (Read-ShareControlBrokerNative -HttpReader { '{"broker":{"running":true}}' })) $true 'an unrelated running field cannot prove control responsiveness'
Assert-Equal ($null -eq (Read-ShareControlBrokerNative -HttpReader { '{}' })) $true 'HTTP missing responsive field remains unknown'
Assert-Code { Read-ShareControlBrokerNative -HttpReader { 'private bad content' } } 'console_status_unavailable' 'bad HTTP JSON does not leak response content'
Assert-Code { Read-ShareControlBrokerNative -HttpReader { throw 'private transport data' } } 'console_status_unavailable' 'HTTP transport failure stays sanitized'

$reloadRunner = {
    param($argv, $seconds)
    $leaf.Arguments = $argv; $leaf.Timeout = $seconds
    return '{"ok":true}'
}.GetNewClosure()
Restart-ShareControlConsoleNative -ProcessRunner $reloadRunner
Assert-Equal (($leaf.Arguments | Select-Object -Last 4) -join ',') '-Action,restart,-Key,usage' 'console reload is bound to usage only'
Assert-Equal $leaf.Timeout 45 'console runtime wrapper has a bounded deadline'
Assert-Code { Restart-ShareControlConsoleNative -ProcessRunner { '{"ok":false}' } } 'console_reload_failed' 'failed console receipt propagates failure'
Assert-Code { Restart-ShareControlConsoleNative -ProcessRunner { '{"ok":"true"}' } } 'console_reload_failed' 'truthy console receipt cannot pass'

$starter = {
    param($hostPath, $argv, $verb, $window)
    $leaf.Host = $hostPath; $leaf.Arguments = $argv; $leaf.Verb = $verb; $leaf.Window = $window
    return 23
}.GetNewClosure()
$quotedEntry = "C:\本机目录\path with space's\Recover-ShareControl.ps1"
$elevatedCode = Invoke-ShareControlElevation -EntryPath $quotedEntry -Starter $starter
Assert-Equal $elevatedCode 23 'native elevation propagates the child exit code'
Assert-Equal $leaf.Host (Join-Path $PSHOME 'pwsh.exe') 'elevation uses the absolute current PS7 host'
Assert-Equal $leaf.Verb 'RunAs' 'elevation uses native UAC'
Assert-Equal $leaf.Window 'Hidden' 'elevated helper does not open an extra console window'
$elevationBody = [System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($leaf.Arguments[-1]))
$parseTokens = $null; $parseErrors = $null
$parsed = [System.Management.Automation.Language.Parser]::ParseInput($elevationBody, [ref]$parseTokens, [ref]$parseErrors)
Assert-Equal $parseErrors.Count 0 'quoted non-ASCII entry path produces valid PowerShell'
$invocation = @($parsed.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true))[0]
Assert-Equal $invocation.CommandElements[0].Value $quotedEntry 'encoded elevation preserves the exact literal entry path'
Assert-Equal ($elevationBody -match '-Action Recover -ConsoleReloaded; exit \$LASTEXITCODE$') $true 'elevated child binds the prepared operator phase and propagates its exit status'

$f = New-Fixture
$r = Invoke-Fixture $f
Assert-Equal $r.ok $true 'recovery confirms broker after restart and console reload'
Assert-Equal (Get-Mutations $f) 'reload:usage,stop:umshare-broker,start:umshare-broker,reload:usage' 'recovery refreshes the console before the fixed broker restart'
Assert-Equal $f.State.Released 1 'successful recovery releases its lock'
Assert-Equal $r.code 'control_recovered' 'recovery does not claim the sharing gateway is open'

$f.State.Events.Clear()
$r = Invoke-Fixture $f
Assert-Equal $r.ok $true 'repeated recovery is safe when control is already healthy'
Assert-Equal (Get-Mutations $f) '' 'healthy control causes no duplicate task starts or reload'

$f = New-Fixture -ConsoleRefreshSufficient $true
$r = Invoke-Fixture $f
Assert-Equal $r.ok $true 'console-only recovery confirms the refreshed client'
Assert-Equal (Get-Mutations $f) 'reload:usage' 'stale console does not restart a responsive broker'

$f = New-Fixture -BrokerState 'Ready' -WatchdogState 'Ready'
$r = Invoke-Fixture $f
Assert-Equal $r.ok $true 'existing stopped broker and watchdog can be started'
Assert-Equal (Get-Mutations $f) 'start:umshare-watchdog,start:umshare-broker,reload:usage' 'watchdog is confirmed before the broker starts'

foreach ($field in @('Denied', 'Missing', 'Foreign')) {
    foreach ($name in @('umshare-broker', 'umshare-watchdog')) {
        $f = New-Fixture
        $f.State[$field] = $name
        $r = Invoke-Fixture $f
        Assert-Equal $r.ok $false "$field $name prevents recovery"
        Assert-Equal (Get-Mutations $f) '' "$field $name cannot cause stop/start/re-registration"
        Assert-Equal $f.State.Released 1 "$field $name releases recovery lock"
    }
}
$f = New-Fixture
$f.State.StopStuck = $true
$r = Invoke-Fixture $f
Assert-Equal $r.code 'broker_stop_unconfirmed' 'unconfirmed stop has a specific failure'
Assert-Equal (Get-Mutations $f) 'reload:usage,stop:umshare-broker' 'broker is not restarted until stop is confirmed'
Assert-Equal ($f.State.Tick -le 20000) $true 'console and stop confirmations have bounded deadlines'

$f = New-Fixture
$f.State.OrphanSurvives = $true
$r = Invoke-Fixture $f
Assert-Equal $r.code 'broker_stop_unconfirmed' 'task Ready alone cannot prove broker process exit'
Assert-Equal (Get-Mutations $f) 'reload:usage,stop:umshare-broker' 'surviving broker process is never killed or duplicated'
$f = New-Fixture
$f.State.ProcessQueryFails = $true
$r = Invoke-Fixture $f
Assert-Equal $r.code 'broker_process_unconfirmed' 'unreadable process identity prevents recovery'
Assert-Equal (Get-Mutations $f) '' 'failed process inventory performs no mutation'
$f = New-Fixture
$f.State.ProcessCount = 2
$r = Invoke-Fixture $f
Assert-Equal $r.code 'broker_process_unconfirmed' 'multiple matching brokers refuse automatic recovery'
Assert-Equal (Get-Mutations $f) '' 'multiple brokers are not killed by PID or task name'

$f = New-Fixture -WatchdogState 'Ready'
$f.State.StartStuck = 'umshare-watchdog'
$r = Invoke-Fixture $f
Assert-Equal $r.code 'watchdog_start_unconfirmed' 'watchdog start is verified rather than assumed'
Assert-Equal (Get-Mutations $f) 'start:umshare-watchdog' 'failed watchdog leaves broker untouched'

$f = New-Fixture
$f.State.StartStuck = 'umshare-broker'
$r = Invoke-Fixture $f
Assert-Equal $r.code 'broker_start_unconfirmed' 'broker start is verified rather than assumed'
Assert-Equal (Get-Mutations $f) 'reload:usage,stop:umshare-broker,start:umshare-broker' 'failed broker is not followed by another reload or success'

$f = New-Fixture
$f.State.MutateOnStop = $true
$r = Invoke-Fixture $f
Assert-Equal $r.code 'task_definition_changed' 'definition change during recovery refuses restart'
Assert-Equal (Get-Mutations $f) 'reload:usage,stop:umshare-broker' 'changed task definition is never adopted'

$f = New-Fixture
$f.State.ReloadOk = $false
$r = Invoke-Fixture $f
Assert-Equal $r.code 'console_reload_failed' 'failed 8001 reload propagates failure'
Assert-Equal $r.ok $false 'running tasks alone cannot pass acceptance'

foreach ($field in @('HttpThrows', 'HttpUnknown')) {
    $f = New-Fixture
    $f.State[$field] = $true
    $r = Invoke-Fixture $f
    Assert-Equal $r.ok $false "$field does not fabricate broker readiness"
    Assert-Equal $r.code 'broker_readiness_unconfirmed' "$field has an honest terminal state"
    Assert-Equal (@($f.State.Events | Where-Object { $_ -eq 'http:broker' }).Count) 3 "$field uses one HTTP read for each recovery phase"
}
$f = New-Fixture -Healthy $true
$r = Invoke-Fixture $f -Action Check
Assert-Equal $r.ok $true 'Check can confirm healthy control'
Assert-Equal (Get-Mutations $f) '' 'Check never changes tasks or reloads console'
Assert-Equal (@($f.State.Events | Where-Object { $_ -eq 'lock' }).Count) 0 'Check does not acquire a write lock'
$f = New-Fixture
$f.State.Denied = 'umshare-broker'
$r = Invoke-Fixture $f -Action Check
Assert-Equal $r.code 'task_access_denied' 'Check distinguishes permission denied from missing'
Assert-Equal (Get-Mutations $f) '' 'unprivileged Check stays read-only'
$f = New-Fixture
$f.State.Locked = $true
$r = Invoke-Fixture $f
Assert-Equal $r.code 'recovery_busy' 'concurrent recovery refuses a second writer'
Assert-Equal (Get-Mutations $f) '' 'concurrent recovery performs no mutation'
Assert-Equal $f.State.Released 0 'failed acquisition does not release another owner lock'

# Entry binding and argument handling use the same function as the actual PS1.
$entryState = @{ CoreCalls = 0; Elevations = 0; CoreAction = ''; Encoded = ''; Reloads = 0 }
$entryAdapter = @{
    IsAdmin = { return $false }
    ReloadConsole = { $entryState.Reloads++ }.GetNewClosure()
    Core = { param($action) $entryState.CoreCalls++; $entryState.CoreAction = $action; return @{ ok = $true; code = 'control_healthy'; events = @() } }.GetNewClosure()
    Elevate = { param($entryPath) $entryState.Elevations++; $entryState.Encoded = $entryPath; return 0 }.GetNewClosure()
}
$r = Invoke-ShareControlEntry -Action Check -EntryPath 'C:\path with spaces\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $entryState.CoreAction 'Check' 'entry Check reaches the read-only core'
Assert-Equal $entryState.Elevations 0 'entry Check never triggers UAC'
$r = Invoke-ShareControlEntry -Action Recover -EntryPath 'C:\path with spaces\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $entryState.Elevations 1 'only explicit Recover requests elevation'
Assert-Equal $entryState.CoreCalls 1 'non-admin Recover does not run privileged core before UAC'
Assert-Equal $entryState.Encoded 'C:\path with spaces\Recover-ShareControl.ps1' 'elevation is bound to the exact entry path'
Assert-Equal $entryState.Reloads 1 'ordinary operator reloads 8001 before UAC'
$entryAdapter.IsAdmin = { return $true }
$r = Invoke-ShareControlEntry -Action Recover -EntryPath 'C:\path\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $r.code 'operator_context_required' 'unprepared elevated entry refuses to launch an elevated console'
$entryAdapter.IsAdmin = { return $false }
$entryAdapter.Elevate = { return 23 }
$r = Invoke-ShareControlEntry -Action Recover -EntryPath 'C:\path\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $r.code 'console_reload_failed' 'elevated child preserves a safe specific failure code'
$entryAdapter.Elevate = { throw 'uac_cancelled' }
$r = Invoke-ShareControlEntry -Action Recover -EntryPath 'C:\path\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $r.code 'uac_cancelled' 'cancelled UAC remains a failure'

$script:blockedElevatedLaunches = 0
Assert-Code { Restart-ShareControlConsoleNative -AdminCheck { return $true } -ProcessRunner {
    $script:blockedElevatedLaunches++
    return '{"ok":true}'
} } 'console_reload_requires_operator' 'native console restart rejects an elevated token'
Assert-Equal $script:blockedElevatedLaunches 0 'elevated guard launches no 8001 process'
$entryAdapter.IsAdmin = { return $true }
$r = Invoke-ShareControlEntry -Action Recover -ConsoleReloaded -EntryPath 'C:\path\Recover-ShareControl.ps1' -Adapter $entryAdapter
Assert-Equal $entryState.CoreAction 'Recover' 'prepared elevated worker reaches the fixed task core'
$f = New-Fixture
$preparedState = $f.State
$f.Adapter.ReadBroker = { return $preparedState.BrokerRestarted }.GetNewClosure()
$r = Invoke-ShareControlRecovery -Action Recover -ConsoleReloaded -Adapter $f.Adapter
Assert-Equal $r.ok $true 'operator-prepared core can restore the broker'
Assert-Equal (Get-Mutations $f) 'stop:umshare-broker,start:umshare-broker' 'prepared elevated core never reloads 8001'

$tokens = $null; $errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'Recover-ShareControl.ps1'), [ref]$tokens, [ref]$errors)
Assert-Equal $errors.Count 0 'real PS1 entry parses without errors'
$entryText = Get-Content -LiteralPath (Join-Path $root 'Recover-ShareControl.ps1') -Encoding utf8 -Raw
Assert-Equal ($entryText -match '#Requires -Version 7\.0') $true 'real PS1 refuses older PowerShell hosts before any work'
Assert-Equal ($entryText -match '\$Action\s*=\s*''Check''') $true 'real PS1 entry defaults to read-only Check'
$batText = Get-Content -LiteralPath (Join-Path $root 'Recover-ShareControl.bat') -Encoding ascii -Raw
Assert-Equal ($batText -match '"%~dp0Recover-ShareControl.ps1" -Action Recover') $true 'BAT uses same-directory fixed Recover entry'
Assert-Equal ($batText -match 'exit /b %') $true 'BAT propagates the entry exit code'

Write-Host "$script:checks checks, $script:failures failures"
if ($script:failures -ne 0) { exit 1 }
exit 0
