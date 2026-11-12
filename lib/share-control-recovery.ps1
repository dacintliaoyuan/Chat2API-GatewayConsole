# Fixed local recovery for the share control plane. Importing this library is
# side-effect free. Scheduler, HTTP, elevation and process work are leaf seams.

function Get-ShareControlErrorCode {
    param([System.Exception]$Exception)
    $safeCodes = @('task_access_denied', 'task_missing', 'task_query_failed',
        'task_identity_mismatch', 'task_definition_changed', 'task_state_unconfirmed',
        'task_operation_failed', 'task_operation_timeout', 'recovery_busy',
        'broker_stop_unconfirmed', 'broker_start_unconfirmed', 'watchdog_start_unconfirmed',
        'broker_readiness_unconfirmed', 'console_reload_failed', 'console_status_unavailable',
        'powershell7_unavailable', 'uac_cancelled', 'elevation_failed', 'windows_required',
        'broker_process_unconfirmed', 'operator_context_required', 'console_reload_requires_operator')
    $cursor = $Exception
    while ($null -ne $cursor) {
        if ($cursor.Message -in $safeCodes) { return $cursor.Message }
        $hex = '{0:X8}' -f $cursor.HResult
        if ($hex -eq '80070005') { return 'task_access_denied' }
        if ($hex -eq '80070002') { return 'task_missing' }
        if ($cursor -is [System.ComponentModel.Win32Exception] -and $cursor.NativeErrorCode -eq 1223) {
            return 'uac_cancelled'
        }
        $cursor = $cursor.InnerException
    }
    return 'task_operation_failed'
}

function Get-ShareControlExitCodes {
    return @{ task_access_denied = 10; task_missing = 11; task_query_failed = 12;
        task_identity_mismatch = 13; task_definition_changed = 14; task_state_unconfirmed = 15;
        task_operation_failed = 16; task_operation_timeout = 17; recovery_busy = 18;
        broker_stop_unconfirmed = 19; broker_start_unconfirmed = 20; watchdog_start_unconfirmed = 21;
        broker_readiness_unconfirmed = 22; console_reload_failed = 23; console_status_unavailable = 24;
        powershell7_unavailable = 25; uac_cancelled = 26; elevation_failed = 27;
        windows_required = 28; control_needs_recovery = 29; broker_process_unconfirmed = 30;
        operator_context_required = 31; console_reload_requires_operator = 32 }
}

function ConvertFrom-ShareControlTaskXml {
    param(
        [ValidateSet('umshare-broker', 'umshare-watchdog')][string]$TaskName,
        [string]$TaskPath, [string]$Xml, [string]$State,
        # Facts from the SAME opened task definition. Missing XML defaults
        # never supply their own authority; every fact is strictly typed.
        [hashtable]$RuntimeFacts
    )
    $facts = @{ task_name = $TaskName; owned = $false; state = $State;
        definition_hash = ''; code = 'task_identity_mismatch' }
    $reader = $null
    try {
        $settings = [System.Xml.XmlReaderSettings]::new()
        $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($Xml), $settings)
        $doc = [System.Xml.XmlDocument]::new()
        $doc.XmlResolver = $null
        $doc.Load($reader)
        $ns = [System.Xml.XmlNamespaceManager]::new($doc.NameTable)
        $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        if ($doc.DocumentElement.LocalName -ne 'Task' -or
            $doc.DocumentElement.NamespaceURI -ne $ns.LookupNamespace('t') -or
            $TaskPath -ine "\$TaskName") { return $facts }
        if ($null -eq $RuntimeFacts -or -not $RuntimeFacts.ContainsKey('principal_logon_type') -or
            -not $RuntimeFacts.ContainsKey('enabled') -or -not $RuntimeFacts.ContainsKey('allow_demand_start') -or
            ($RuntimeFacts.principal_logon_type -isnot [int] -and $RuntimeFacts.principal_logon_type -isnot [long]) -or
            $RuntimeFacts.enabled -isnot [bool] -or -not $RuntimeFacts.enabled -or
            $RuntimeFacts.allow_demand_start -isnot [bool] -or -not $RuntimeFacts.allow_demand_start) { return $facts }
        $allowedLogonTypes = if ($TaskName -eq 'umshare-broker') { @(2, 5) } else { @(5) }
        if ($RuntimeFacts.principal_logon_type -notin $allowedLogonTypes) { return $facts }
        $principals = @($doc.SelectNodes('/t:Task/t:Principals/t:Principal', $ns))
        $actions = @($doc.SelectNodes('/t:Task/t:Actions/*', $ns))
        if ($principals.Count -ne 1 -or $actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { return $facts }
        $user = $principals[0].SelectSingleNode('t:UserId', $ns)
        if ($null -eq $user -or $null -ne $principals[0].SelectSingleNode('t:GroupId', $ns)) { return $facts }
        $sid = $user.InnerText
        if ($sid -ne 'S-1-5-18') {
            $sid = ([System.Security.Principal.NTAccount]::new($sid)).Translate(
                [System.Security.Principal.SecurityIdentifier]).Value
        }
        if ($sid -ne 'S-1-5-18') { return $facts }
        $logon = $principals[0].SelectSingleNode('t:LogonType', $ns)
        $runLevel = $principals[0].SelectSingleNode('t:RunLevel', $ns)
        if ($null -eq $runLevel -or $runLevel.InnerText -ne 'HighestAvailable') { return $facts }
        if ($null -ne $logon) {
            $logonTypes = @{ S4U = 2; ServiceAccount = 5 }
            if (-not $logonTypes.ContainsKey($logon.InnerText) -or
                $logonTypes[$logon.InnerText] -ne $RuntimeFacts.principal_logon_type) { return $facts }
        }
        $command = $actions[0].SelectSingleNode('t:Command', $ns)
        $arguments = $actions[0].SelectSingleNode('t:Arguments', $ns)
        $working = $actions[0].SelectSingleNode('t:WorkingDirectory', $ns)
        $runRoot = 'C:\ProgramData\UMShareRun'
        $expectedCommand = "$runRoot\run-broker.cmd"
        $expectedArguments = ''
        if ($TaskName -eq 'umshare-watchdog') {
            $expectedCommand = Join-Path $PSHOME 'pwsh.exe'
            $expectedArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $runRoot + '\watchdog-umshare.ps1"'
        }
        if ($null -eq $command -or $command.InnerText -ine $expectedCommand -or
            $null -eq $working -or $working.InnerText.TrimEnd('\') -ine $runRoot) { return $facts }
        $actualArguments = if ($null -eq $arguments) { '' } else { $arguments.InnerText }
        if ($actualArguments -cne $expectedArguments) { return $facts }
        $enabled = $doc.SelectSingleNode('/t:Task/t:Settings/t:Enabled', $ns)
        $instances = $doc.SelectSingleNode('/t:Task/t:Settings/t:MultipleInstancesPolicy', $ns)
        $onDemand = $doc.SelectSingleNode('/t:Task/t:Settings/t:AllowStartOnDemand', $ns)
        if (($null -ne $enabled -and $enabled.InnerText -ne 'true') -or
            $null -eq $instances -or $instances.InnerText -ne 'IgnoreNew' -or
            ($null -ne $onDemand -and $onDemand.InnerText -ne 'true')) { return $facts }
        $facts.definition_hash = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData(
            [System.Text.Encoding]::UTF8.GetBytes($Xml))).ToLowerInvariant()
        $facts.owned = $true
        $facts.code = 'ok'
        return $facts
    } catch {
        return $facts
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
    }
}

function Invoke-ShareControlTaskWorker {
    # Runs only in a bounded, hidden PS7 child. Exact paths bypass the
    # filtered enumeration that hides protected tasks from ordinary users.
    param(
        [ValidateSet('Read', 'Start', 'Stop')][string]$Operation,
        [ValidateSet('umshare-broker', 'umshare-watchdog')][string]$TaskName,
        [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$ExpectedHash = ''
    )
    $scheduler = $null; $folder = $null; $task = $null
    $definition = $null; $principal = $null; $settings = $null
    try {
        $scheduler = New-Object -ComObject 'Schedule.Service'
        $scheduler.Connect()
        $folder = $scheduler.GetFolder('\')
        $task = $folder.GetTask($TaskName)
        $states = @{ 0 = 'Unknown'; 1 = 'Disabled'; 2 = 'Queued'; 3 = 'Ready'; 4 = 'Running' }
        $number = [int]$task.State
        $state = if ($states.ContainsKey($number)) { $states[$number] } else { 'Unknown' }
        try {
            $definition = $task.Definition
            $principal = $definition.Principal
            $settings = $definition.Settings
            $runtimeFacts = @{ principal_logon_type = $principal.LogonType;
                enabled = $settings.Enabled; allow_demand_start = $settings.AllowDemandStart }
        } catch { throw 'task_identity_mismatch' }
        # Bind the complete XML and runtime facts to one definition snapshot.
        $facts = ConvertFrom-ShareControlTaskXml -TaskName $TaskName -TaskPath ([string]$task.Path) -Xml ([string]$definition.XmlText) -State $state -RuntimeFacts $runtimeFacts
        if ($Operation -ne 'Read') {
            if ($facts.owned -ne $true) { throw 'task_identity_mismatch' }
            if ($ExpectedHash -notmatch '^[0-9a-f]{64}$' -or $facts.definition_hash -cne $ExpectedHash) { throw 'task_definition_changed' }
            if ($state -notin @('Ready', 'Queued', 'Running')) { throw 'task_state_unconfirmed' }
            if ($Operation -eq 'Stop' -and $state -ne 'Ready') { $task.Stop(0) }
            if ($Operation -eq 'Start' -and $state -eq 'Ready') { [void]$task.Run($null) }
        }
        return @{ ok = $true; facts = $facts }
    } catch {
        return @{ ok = $false; code = (Get-ShareControlErrorCode $_.Exception) }
    } finally {
        foreach ($com in @($settings, $principal, $definition, $task, $folder, $scheduler)) {
            if ($null -ne $com -and [System.Runtime.InteropServices.Marshal]::IsComObject($com)) {
                [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($com)
            }
        }
    }
}

function Get-ShareControlPowerShell {
    if (-not $IsWindows) { throw 'windows_required' }
    $hostPath = Join-Path $PSHOME 'pwsh.exe'
    if ($PSVersionTable.PSVersion.Major -lt 7 -or -not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
        throw 'powershell7_unavailable'
    }
    return $hostPath
}

function Get-ShareControlBrokerProcessWorker {
    # Query only. A task becoming Ready cannot prove that its broker exited.
    param([scriptblock]$ProcessReader, [scriptblock]$OwnerReader)
    try {
        $runRoot = 'C:\ProgramData\UMShareRun'
        $image = "$runRoot\um-share-python.exe"
        $serverToken = '(?:^|\s)"?' + [regex]::Escape("$runRoot\share/broker_server.py") + '"?(?=\s|$)'
        $count = 0
        $rows = if ($null -eq $ProcessReader) {
            @(Get-CimInstance Win32_Process -Filter "Name='um-share-python.exe'" -ErrorAction Stop)
        } else { @(& $ProcessReader) }
        foreach ($row in $rows) {
            if (-not $row.ExecutablePath -or -not $row.CommandLine) { throw 'broker_process_unconfirmed' }
            if ([string]$row.ExecutablePath -ine $image) { continue }
            if ([string]$row.CommandLine -notmatch '(?i)share_broker_server\.py') { continue }
            if ([string]$row.CommandLine -notmatch $serverToken -or -not $row.CreationDate -or [int]$row.ProcessId -le 0) { throw 'broker_process_unconfirmed' }
            $owner = if ($null -eq $OwnerReader) {
                Invoke-CimMethod -InputObject $row -MethodName GetOwnerSid -ErrorAction Stop
            } else { & $OwnerReader $row }
            if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne 'S-1-5-18') { throw 'broker_process_unconfirmed' }
            $count++
        }
        return @{ ok = $true; count = $count }
    } catch {
        return @{ ok = $false; code = 'broker_process_unconfirmed' }
    }
}

function Invoke-ShareControlProcess {
    param([string[]]$Arguments, [int]$TimeoutSeconds = 6)
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Get-ShareControlPowerShell
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $start.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $child = [System.Diagnostics.Process]::new()
    $child.StartInfo = $start
    try {
        [void]$child.Start()
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        if (-not $child.WaitForExit($TimeoutSeconds * 1000)) {
            # Only this helper is terminated. No SYSTEM task PID is killed.
            $child.Kill()
            [void]$child.WaitForExit(1000)
            throw 'task_operation_timeout'
        }
        # A grandchild must not hold inherited pipe handles open forever.
        try {
            $text = $stdout.WaitAsync([TimeSpan]::FromSeconds(1)).GetAwaiter().GetResult()
            [void]$stderr.WaitAsync([TimeSpan]::FromSeconds(1)).GetAwaiter().GetResult()
        } catch { throw 'task_operation_timeout' }
        if ($child.ExitCode -ne 0 -or $text.Length -gt 65536) { throw 'task_operation_failed' }
        return $text
    } finally {
        $child.Dispose()
    }
}

function Invoke-ShareControlTaskNative {
    param(
        [ValidateSet('Read', 'Start', 'Stop')][string]$Operation,
        [ValidateSet('umshare-broker', 'umshare-watchdog')][string]$TaskName,
        [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$ExpectedHash = '', [scriptblock]$ProcessRunner
    )
    if ($Operation -ne 'Read' -and $ExpectedHash -notmatch '^[0-9a-f]{64}$') { throw 'task_definition_changed' }
    $library = (Join-Path $PSScriptRoot 'share-control-recovery.ps1').Replace("'", "''")
    $body = "`$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(`$false); . '$library'; Invoke-ShareControlTaskWorker -Operation '$Operation' -TaskName '$TaskName' -ExpectedHash '$ExpectedHash' | ConvertTo-Json -Depth 5 -Compress"
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($body))
    $arguments = @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded)
    $raw = if ($null -eq $ProcessRunner) { Invoke-ShareControlProcess -Arguments $arguments } else { & $ProcessRunner $arguments 6 }
    try { $result = ConvertFrom-Json -InputObject $raw -AsHashtable -ErrorAction Stop } catch { throw 'task_query_failed' }
    if ($result -isnot [System.Collections.IDictionary] -or -not $result.Contains('ok')) { throw 'task_query_failed' }
    if ($result.ok -isnot [bool] -or -not $result.ok) {
        $code = if ($result.Contains('code')) { [string]$result.code } else { 'task_query_failed' }
        throw (Get-ShareControlErrorCode ([System.Exception]::new($code)))
    }
    if ($Operation -eq 'Read') { return $result.facts }
}

function Read-ShareControlBrokerProcessCountNative {
    param([scriptblock]$ProcessRunner)
    $library = (Join-Path $PSScriptRoot 'share-control-recovery.ps1').Replace("'", "''")
    $body = "`$ErrorActionPreference='Stop'; . '$library'; Get-ShareControlBrokerProcessWorker | ConvertTo-Json -Compress"
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($body))
    $arguments = @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded)
    try {
        $raw = if ($null -eq $ProcessRunner) { Invoke-ShareControlProcess -Arguments $arguments } else { & $ProcessRunner $arguments 6 }
        $result = $raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        if ($result -isnot [System.Collections.IDictionary] -or -not $result.Contains('ok') -or
            $result.ok -isnot [bool] -or -not $result.ok -or -not $result.Contains('count') -or
            ($result.count -isnot [int] -and $result.count -isnot [long]) -or $result.count -lt 0) { throw 'broker_process_unconfirmed' }
        return [int]$result.count
    } catch { throw 'broker_process_unconfirmed' }
}

function Read-ShareControlBrokerNative {
    param([scriptblock]$HttpReader)
    $url = 'http://127.0.0.1:8001/api/console/share-management/control-status'
    $client = $null; $response = $null
    try {
        if ($null -eq $HttpReader) {
            $handler = [System.Net.Http.HttpClientHandler]::new()
            $handler.UseProxy = $false
            $handler.AllowAutoRedirect = $false
            $client = [System.Net.Http.HttpClient]::new($handler)
            $client.Timeout = [TimeSpan]::FromSeconds(15)
            $client.MaxResponseContentBufferSize = 65536
            $response = $client.GetAsync($url).GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) { throw 'console_status_unavailable' }
            $raw = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        } else {
            $raw = & $HttpReader $url 15
        }
        if ($raw -isnot [string] -or $raw.Length -gt 65536) { throw 'console_status_unavailable' }
        $payload = $raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        if ($payload -is [System.Collections.IDictionary] -and $payload.Contains('responsive') -and
            $payload.responsive -is [bool]) { return $payload.responsive }
        return $null
    } catch {
        throw 'console_status_unavailable'
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Test-ShareControlAdministrator {
    if (-not $IsWindows) { throw 'windows_required' }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    try { return ([System.Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
}

function Restart-ShareControlConsoleNative {
    param([scriptblock]$ProcessRunner, [scriptblock]$AdminCheck)
    $elevated = if ($null -ne $AdminCheck) { & $AdminCheck } else { Test-ShareControlAdministrator }
    if ($elevated) { throw 'console_reload_requires_operator' }
    $entry = Join-Path (Split-Path -Parent $PSScriptRoot) 'invoke-console-runtime-json.ps1'
    try {
        $arguments = @('-NoProfile', '-NonInteractive', '-File', $entry, '-Action', 'restart', '-Key', 'usage')
        $raw = if ($null -eq $ProcessRunner) { Invoke-ShareControlProcess -Arguments $arguments -TimeoutSeconds 45 } else { & $ProcessRunner $arguments 45 }
        $result = $raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        if ($result -isnot [System.Collections.IDictionary] -or -not $result.Contains('ok') -or $result.ok -isnot [bool] -or -not $result.ok) { throw 'console_reload_failed' }
    } catch { throw 'console_reload_failed' }
}

function New-ShareControlNativeAdapter {
    return @{
        ReadTask = { param($name) Invoke-ShareControlTaskNative -Operation Read -TaskName $name }
        StopTask = { param($name, $hash) Invoke-ShareControlTaskNative -Operation Stop -TaskName $name -ExpectedHash $hash }
        StartTask = { param($name, $hash) Invoke-ShareControlTaskNative -Operation Start -TaskName $name -ExpectedHash $hash }
        ReadBrokerProcessCount = { Read-ShareControlBrokerProcessCountNative }
        ReloadConsole = { Restart-ShareControlConsoleNative }
        ReadBroker = { Read-ShareControlBrokerNative }
        AcquireLock = {
            $mutex = [System.Threading.Mutex]::new($false, 'Global\SelfUseAI-UMShareControlRecovery')
            try {
                $acquired = $false
                try { $acquired = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
                if (-not $acquired) { throw 'recovery_busy' }
                return $mutex
            } catch { $mutex.Dispose(); throw }
        }
        ReleaseLock = { param($mutex) try { $mutex.ReleaseMutex() } finally { $mutex.Dispose() } }
        Now = { return [Environment]::TickCount64 }
        Delay = { param($milliseconds) Start-Sleep -Milliseconds $milliseconds }
    }
}

function Invoke-ShareControlRecovery {
    param([ValidateSet('Check', 'Recover')][string]$Action = 'Check', [hashtable]$Adapter,
          [switch]$ConsoleReloaded)
    if ($null -eq $Adapter) { $Adapter = New-ShareControlNativeAdapter }
    $events = [System.Collections.Generic.List[object]]::new()
    $lock = $null; $stage = 'inventory'
    function Read-OwnedTask {
        param([string]$Name, [string]$ExpectedHash = '')
        $facts = & $Adapter.ReadTask $Name
        if ($facts -isnot [System.Collections.IDictionary] -or $facts.task_name -cne $Name -or
            $facts.owned -isnot [bool] -or -not $facts.owned -or $facts.definition_hash -notmatch '^[0-9a-f]{64}$') { throw 'task_identity_mismatch' }
        if ($ExpectedHash -and $facts.definition_hash -cne $ExpectedHash) { throw 'task_definition_changed' }
        if ($facts.state -notin @('Ready', 'Running', 'Queued')) { throw 'task_state_unconfirmed' }
        return $facts
    }
    function Wait-OwnedTask {
        param([string]$Name, [string]$Hash, [string]$WantedState, [string]$Failure)
        $deadline = (& $Adapter.Now) + 10000
        do {
            $facts = Read-OwnedTask $Name $Hash
            if ($facts.state -eq $WantedState) { return $facts }
            if ((& $Adapter.Now) -ge $deadline) { throw $Failure }
            & $Adapter.Delay 250
        } while ($true)
    }
    function Test-BrokerReady {
        # One bounded request per phase. Do not add a retry loop around the
        # HTTP adapter's complete 15-second request budget.
        try { $ready = & $Adapter.ReadBroker; return ($ready -is [bool] -and $ready) } catch { return $false }
    }
    function Read-BrokerProcessCount {
        $count = & $Adapter.ReadBrokerProcessCount
        if (($count -isnot [int] -and $count -isnot [long]) -or $count -lt 0 -or $count -gt 1) { throw 'broker_process_unconfirmed' }
        return $count
    }
    function Wait-BrokerProcessCount {
        param([int]$WantedCount, [string]$Failure)
        $deadline = (& $Adapter.Now) + 10000
        do {
            if ((Read-BrokerProcessCount) -eq $WantedCount) { return }
            if ((& $Adapter.Now) -ge $deadline) { throw $Failure }
            & $Adapter.Delay 250
        } while ($true)
    }
    try {
        if ($Action -eq 'Recover') { $stage = 'lock'; $lock = & $Adapter.AcquireLock }
        $stage = 'inventory'
        $broker = Read-OwnedTask 'umshare-broker'
        $watchdog = Read-OwnedTask 'umshare-watchdog'
        $processCount = Read-BrokerProcessCount
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'fixed_tasks_owned' })
        $ready = Test-BrokerReady
        if ($ready -and $processCount -eq 1 -and $broker.state -eq 'Running' -and $watchdog.state -eq 'Running') {
            return @{ ok = $true; code = 'control_healthy'; events = $events.ToArray() }
        }
        if ($Action -eq 'Check') {
            return @{ ok = $false; code = 'control_needs_recovery'; events = $events.ToArray() }
        }
        if ($broker.state -eq 'Running' -and $watchdog.state -eq 'Running') {
            # Refresh a stale console client before touching an existing
            # broker. A missing snapshot never proves the process stopped.
            $stage = 'console_reload'
            if (-not $ConsoleReloaded) { & $Adapter.ReloadConsole | Out-Null }
            $reloadCode = if ($ConsoleReloaded) { 'operator_reloaded' } else { 'reloaded' }
            $events.Add(@{ stage = $stage; state = 'confirmed'; code = $reloadCode })
            $stage = 'broker_verify'
            if (Test-BrokerReady) {
                $broker = Read-OwnedTask 'umshare-broker' $broker.definition_hash
                $watchdog = Read-OwnedTask 'umshare-watchdog' $watchdog.definition_hash
                if ($broker.state -eq 'Running' -and $watchdog.state -eq 'Running' -and (Read-BrokerProcessCount) -eq 1) {
                    $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'responsive' })
                    return @{ ok = $true; code = 'control_recovered'; events = $events.ToArray() }
                }
                throw 'broker_readiness_unconfirmed'
            }
            $events.Add(@{ stage = $stage; state = 'unconfirmed'; code = 'broker_readiness_unconfirmed' })
        }
        $stage = 'watchdog'
        if ($watchdog.state -eq 'Ready') { & $Adapter.StartTask 'umshare-watchdog' $watchdog.definition_hash | Out-Null }
        $watchdog = Wait-OwnedTask 'umshare-watchdog' $watchdog.definition_hash 'Running' 'watchdog_start_unconfirmed'
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'running' })
        $stage = 'broker_stop'
        $broker = Read-OwnedTask 'umshare-broker' $broker.definition_hash
        if ($broker.state -ne 'Ready') { & $Adapter.StopTask 'umshare-broker' $broker.definition_hash | Out-Null }
        $broker = Wait-OwnedTask 'umshare-broker' $broker.definition_hash 'Ready' 'broker_stop_unconfirmed'
        Wait-BrokerProcessCount 0 'broker_stop_unconfirmed'
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'stopped' })
        $stage = 'broker_start'
        & $Adapter.StartTask 'umshare-broker' $broker.definition_hash | Out-Null
        $broker = Wait-OwnedTask 'umshare-broker' $broker.definition_hash 'Running' 'broker_start_unconfirmed'
        Wait-BrokerProcessCount 1 'broker_start_unconfirmed'
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'running' })
        $stage = 'console_reload'
        if (-not $ConsoleReloaded) { & $Adapter.ReloadConsole | Out-Null }
        $reloadCode = if ($ConsoleReloaded) { 'operator_reloaded' } else { 'reloaded' }
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = $reloadCode })
        $stage = 'broker_verify'
        if (-not (Test-BrokerReady)) { throw 'broker_readiness_unconfirmed' }
        $broker = Read-OwnedTask 'umshare-broker' $broker.definition_hash
        $watchdog = Read-OwnedTask 'umshare-watchdog' $watchdog.definition_hash
        if ($broker.state -ne 'Running' -or $watchdog.state -ne 'Running' -or (Read-BrokerProcessCount) -ne 1) { throw 'broker_readiness_unconfirmed' }
        $events.Add(@{ stage = $stage; state = 'confirmed'; code = 'responsive' })
        return @{ ok = $true; code = 'control_recovered'; events = $events.ToArray() }
    } catch {
        $code = Get-ShareControlErrorCode $_.Exception
        $events.Add(@{ stage = $stage; state = 'unconfirmed'; code = $code })
        return @{ ok = $false; code = $code; events = $events.ToArray() }
    } finally {
        if ($null -ne $lock) { & $Adapter.ReleaseLock $lock | Out-Null }
    }
}

function Invoke-ShareControlElevation {
    param([string]$EntryPath, [scriptblock]$Starter)
    # EncodedCommand preserves spaces, quotes and non-ASCII paths without
    # building a shell command or accepting arbitrary recovery arguments.
    $literal = $EntryPath.Replace("'", "''")
    $body = "& '$literal' -Action Recover -ConsoleReloaded; exit `$LASTEXITCODE"
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($body))
    $hostPath = Get-ShareControlPowerShell
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
    if ($null -ne $Starter) { return & $Starter $hostPath $arguments 'RunAs' 'Hidden' }
    # Native UAC remains visible; the worker stays hidden and the outer BAT
    # reports its safe terminal code without opening another console window.
    $child = Start-Process -FilePath $hostPath -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait
    try { return $child.ExitCode } finally { $child.Dispose() }
}

function Invoke-ShareControlEntry {
    param([ValidateSet('Check', 'Recover')][string]$Action = 'Check', [string]$EntryPath, [hashtable]$Adapter,
          [switch]$ConsoleReloaded)
    if ($null -eq $Adapter) {
        $Adapter = @{
            IsAdmin = { Test-ShareControlAdministrator }
            ReloadConsole = { Restart-ShareControlConsoleNative }
            Elevate = { param($path) Invoke-ShareControlElevation $path }
            Core = { param($action, $prepared) Invoke-ShareControlRecovery -Action $action -ConsoleReloaded:$prepared }
        }
    }
    try {
        if ($Action -eq 'Recover' -and -not (& $Adapter.IsAdmin)) {
            # This ordinary parent owns 8001. The elevated worker only owns
            # the protected tasks and must never launch an elevated console.
            & $Adapter.ReloadConsole | Out-Null
            $exitCode = & $Adapter.Elevate $EntryPath
            $code = 'elevation_failed'
            foreach ($item in (Get-ShareControlExitCodes).GetEnumerator()) {
                if ($item.Value -eq $exitCode) { $code = $item.Key; break }
            }
            if ($exitCode -eq 0) { $code = 'control_recovered' }
            return @{ ok = ($exitCode -eq 0); code = $code; events = @() }
        }
        if ($Action -eq 'Recover' -and -not $ConsoleReloaded) {
            throw 'operator_context_required'
        }
        return & $Adapter.Core $Action ([bool]$ConsoleReloaded)
    } catch {
        return @{ ok = $false; code = (Get-ShareControlErrorCode $_.Exception); events = @() }
    }
}
