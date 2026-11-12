# Offline regression tests for the UM cloud console desktop entry.
#
# Every external boundary is replaced with an in-memory fake: ssh resolution and
# `ssh -G`, tailscale status, TCP listener and process queries, tunnel start and
# stop, HTTP health, Edge launch, COM shortcut creation, the entry mutex and sleep.
# No real ssh, tailscale, Edge or network request is used. Transport checks also
# exercise a harmless local PowerShell child and its bounded timeout cleanup.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $root 'Open-CloudConsole.ps1'
$bat = Join-Path $root 'Open-CloudConsole.bat'
$projectRoot = Split-Path -Parent $root
$gitignore = Join-Path $projectRoot '.gitignore'

if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    Write-Host 'FAIL cloud console entry is not implemented' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $bat -PathType Leaf)) {
    Write-Host 'FAIL cloud console bat entry is not implemented' -ForegroundColor Red
    exit 1
}
. $entry -Library

$script:failures = 0
$script:checks = 0

function Assert-Equal {
    param($Actual, $Expected, [string]$Name)
    $script:checks++
    if ([object]::Equals($Actual, $Expected)) {
        Write-Host "PASS $Name"
        return
    }
    $script:failures++
    Write-Host "FAIL $Name : expected '$Expected', got '$Actual'" -ForegroundColor Red
}

function Assert-True {
    param($Condition, [string]$Name)
    $script:checks++
    if ([bool]$Condition) {
        Write-Host "PASS $Name"
        return
    }
    $script:failures++
    Write-Host "FAIL $Name : condition was false" -ForegroundColor Red
}

function Assert-Match {
    param([string]$Text, [string]$Pattern, [string]$Name)
    Assert-True ($Text -match $Pattern) $Name
}

function Assert-NotMatch {
    param([string]$Text, [string]$Pattern, [string]$Name)
    Assert-True ($Text -notmatch $Pattern) $Name
}

$script:tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('cloud-console-entry-' + [Guid]::NewGuid().ToString('N'))
$script:statePath = Join-Path $script:tempRoot 'state/cloud-console-tunnel.json'
$script:logPath = Join-Path $script:tempRoot 'logs/cloud-console-entry.log'

function New-FakeTunnelCommandLine {
    param(
        [string]$ConfigPath = $script:fake.ConfigPath,
        [string]$HostAlias = 'um-share-ecs',
        [string]$Forward = '127.0.0.1:18096:127.0.0.1:18096'
    )
    return '"' + $script:fake.SshExe + '" -F "' + $ConfigPath + '" -N -L ' + $Forward + ' ' + $HostAlias
}

function Write-FakeState {
    param(
        [int]$ProcessId,
        [string]$ConfigPath = $script:fake.ConfigPath,
        [string]$HostAlias = 'um-share-ecs',
        [string]$Route = 'private'
    )
    $payload = [ordered]@{
        pid = $ProcessId
        started_utc = '2026-11-04T00:00:00Z'
        bind = '127.0.0.1'
        port = 18096
        target = '127.0.0.1:18096'
        route = $Route
        ssh_exe = $script:fake.SshExe
        ssh_config = $ConfigPath
        ssh_host = $HostAlias
        command_line = (New-FakeTunnelCommandLine -ConfigPath $ConfigPath -HostAlias $HostAlias)
    } | ConvertTo-Json -Depth 4
    $directory = Split-Path -Parent $script:statePath
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    [IO.File]::WriteAllText($script:statePath, $payload, [Text.UTF8Encoding]::new($false))
}

function Add-OwnTunnel {
    param(
        [int]$ProcessId = 4200,
        [string]$ConfigPath = $script:fake.ConfigPath,
        [string]$HostAlias = 'um-share-ecs',
        [string]$Name = 'ssh.exe'
    )
    $commandLine = New-FakeTunnelCommandLine -ConfigPath $ConfigPath -HostAlias $HostAlias
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = $ProcessId })
    $script:fake.Processes[$ProcessId] = @{
        ProcessId = $ProcessId; Name = $Name; CommandLine = $commandLine; Alive = $true
    }
}

function Reset-Fake {
    if (Test-Path -LiteralPath $script:tempRoot) {
        Remove-Item -LiteralPath $script:tempRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $script:tempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:tempRoot 'state') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:tempRoot 'logs') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:tempRoot 'Desktop') -Force | Out-Null

    $config = Join-Path $script:tempRoot 'aliyun-um-ecs.conf'
    $identity = Join-Path $script:tempRoot 'aliyun-um-ecs-ed25519'
    $knownHosts = Join-Path $script:tempRoot 'aliyun-um-ecs_known_hosts'
    $publicConfig = Join-Path $script:tempRoot 'aliyun-um-ecs-public.conf'
    $publicIdentity = Join-Path $script:tempRoot 'aliyun-um-ecs-public-ed25519'
    $publicKnownHosts = Join-Path $script:tempRoot 'aliyun-um-ecs-public_known_hosts'
    $sentinel = 'SENTINEL-SECRET'
    [IO.File]::WriteAllText($config, "# $sentinel config", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($identity,
        "-----BEGIN OPENSSH PRIVATE KEY-----`n$sentinel`n-----END OPENSSH PRIVATE KEY-----",
        [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($knownHosts, "known host $sentinel", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($publicConfig, "# $sentinel public config", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($publicIdentity,
        "-----BEGIN OPENSSH PRIVATE KEY-----`n$sentinel`n-----END OPENSSH PRIVATE KEY-----",
        [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($publicKnownHosts, "public known host $sentinel", [Text.UTF8Encoding]::new($false))

    $script:fake = @{
        SshExe = 'C:\fake\ssh.exe'
        SshMissing = $false
        ConfigPath = $config
        IdentityPath = $identity
        KnownHostsPath = $knownHosts
        PublicConfigPath = $publicConfig
        PublicIdentityPath = $publicIdentity
        PublicKnownHostsPath = $publicKnownHosts
        BatPath = Join-Path $root 'Open-CloudConsole.bat'
        DesktopPath = Join-Path $script:tempRoot 'Desktop'
        ConfigExists = $true
        IdentityExists = $true
        KnownHostsExists = $true
        PublicConfigExists = $false
        PublicIdentityExists = $true
        PublicKnownHostsExists = $true
        BatExists = $true
        EffectiveCalls = 0
        EffectiveThrows = $false
        PublicEffectiveCalls = 0
        PublicEffectiveThrows = $false
        EffectiveConfig = @{
            batchmode = @('yes')
            stricthostkeychecking = @('true')
            identitiesonly = @('yes')
            identityfile = @($identity)
            userknownhostsfile = @($knownHosts)
            hostname = @('100.71.250.7')
            user = @('root')
            port = @('22')
            connecttimeout = @('15')
            serveraliveinterval = @('30')
            serveralivecountmax = @('3')
            tcpkeepalive = @('yes')
        }
        PublicEffectiveConfig = @{
            batchmode = @('yes')
            stricthostkeychecking = @('true')
            identitiesonly = @('yes')
            identityfile = @($publicIdentity)
            userknownhostsfile = @($publicKnownHosts)
            hostname = @('ecs.example.com')
            user = @('root')
            port = @('22')
            connecttimeout = @('15')
            serveraliveinterval = @('30')
            serveralivecountmax = @('3')
            tcpkeepalive = @('yes')
        }
        ResolveDnsName = @{ Ok = $true; Addresses = @('203.0.113.10'); Detail = '' }
        ResolveDnsNameThrows = $false
        ResolveDnsNameCalls = 0
        ResolveDnsNameNames = [System.Collections.Generic.List[string]]::new()
        HostsOverride = @{ Exists = $true; Override = $false;
            Path = 'C:\Windows\System32\drivers\etc\hosts'; Lines = @(); Detail = '' }
        HostsOverrideThrows = $false
        HostsOverrideCalls = 0
        HostsOverrideDomains = [System.Collections.Generic.List[string]]::new()
        Tailscale = @{
            Available = $true; BackendState = 'Running'; SelfOnline = $true
            PeerFound = $true; PeerOnline = $true; Detail = ''
        }
        TailscaleThrows = $false
        TailscaleCalls = 0
        Listeners = @()
        ListenerWhenBound = @()
        BindAfterPoll = 0
        PollCount = 0
        ListenerCalls = 0
        ListenerThrows = $false
        Processes = @{}
        ProcessCalls = 0
        ProcessThrows = $false
        ProcessDiesAfterPoll = 0
        NextPid = 4100
        StartCalls = 0
        StartThrows = $false
        StartThrowsOnce = $false
        StartExits = $false
        StartReturnsNull = $false
        StartReturnsZero = $false
        BindOnStart = $true
        ArgumentLine = ''
        StdoutPath = ''
        StderrPath = ''
        StderrText = ''
        StopCalls = 0
        StoppedPids = [System.Collections.Generic.List[int]]::new()
        StopSucceeds = $true
        StopThrows = $false
        StopLeavesAlive = $false
        StopExitsProcess = $false
        HealthCalls = 0
        HealthThrows = $false
        HealthSequence = @()
        Health = @{ Ok = $true; StatusCode = 200;
            Content = '{"status":"ok","service":"usagesrv","port":18096}'; Detail = '' }
        HealthUris = [System.Collections.Generic.List[string]]::new()
        BootstrapCalls = 0
        BootstrapArgumentLine = ''
        BootstrapSshExe = ''
        BootstrapThrows = $false
        BootstrapResponse = @{ ExitCode = 0; Output = '/display/grant#console=ConsoleBootstrap_0123456789' }
        BrowserCalls = 0
        BrowserThrows = $false
        BrowserReturnsFalse = $false
        LastUrl = ''
        LockCalls = 0
        LockAvailable = $true
        LockNames = [System.Collections.Generic.List[string]]::new()
        UnlockCalls = 0
        SleepCalls = 0
        PathChecks = [System.Collections.Generic.List[string]]::new()
        SaveCalls = 0
        CreateCalls = 0
        ShortcutPath = ''
    }

    $shortcut = [pscustomobject]@{
        TargetPath = ''
        Arguments = ''
        WorkingDirectory = ''
        Description = ''
        IconLocation = ''
    }
    Add-Member -InputObject $shortcut -MemberType ScriptMethod -Name Save -Value {
        $script:fake.SaveCalls++
    }
    $shell = [pscustomobject]@{}
    Add-Member -InputObject $shell -MemberType ScriptMethod -Name CreateShortcut -Value {
        param($path)
        $script:fake.CreateCalls++
        $script:fake.ShortcutPath = $path
        return $script:fake.Shortcut
    }
    $script:fake.Shortcut = $shortcut
    $script:fake.Shell = $shell
}

function New-TestAdapter {
    return @{
        ResolveSsh = {
            if ($script:fake.SshMissing) { throw 'ssh.exe missing' }
            return $script:fake.SshExe
        }
        GetEffectiveConfig = {
            param($SshExe, $ConfigPath, $HostAlias)
            if ($ConfigPath -eq $script:fake.PublicConfigPath) {
                $script:fake.PublicEffectiveCalls++
                if ($script:fake.PublicEffectiveThrows) { throw 'public ssh -G failed' }
                return $script:fake.PublicEffectiveConfig
            }
            $script:fake.EffectiveCalls++
            if ($script:fake.EffectiveThrows) { throw 'ssh -G failed' }
            return $script:fake.EffectiveConfig
        }
        TestPath = {
            param($Path)
            $script:fake.PathChecks.Add([string]$Path)
            if ($Path -eq $script:fake.ConfigPath) { return $script:fake.ConfigExists }
            if ($Path -eq $script:fake.IdentityPath) { return $script:fake.IdentityExists }
            if ($Path -eq $script:fake.KnownHostsPath) { return $script:fake.KnownHostsExists }
            if ($Path -eq $script:fake.PublicConfigPath) { return $script:fake.PublicConfigExists }
            if ($Path -eq $script:fake.PublicIdentityPath) { return $script:fake.PublicIdentityExists }
            if ($Path -eq $script:fake.PublicKnownHostsPath) { return $script:fake.PublicKnownHostsExists }
            if ($Path -eq $script:fake.BatPath) { return $script:fake.BatExists }
            return $false
        }
        GetListeners = {
            param($Port)
            $script:fake.ListenerCalls++
            if ($script:fake.ListenerThrows) { throw 'listener query failed' }
            $script:fake.PollCount++
            if ($script:fake.BindAfterPoll -gt 0 -and
                    $script:fake.PollCount -ge $script:fake.BindAfterPoll) {
                return @($script:fake.ListenerWhenBound)
            }
            return @($script:fake.Listeners)
        }
        GetProcess = {
            param($ProcessId)
            $script:fake.ProcessCalls++
            if ($script:fake.ProcessThrows) { throw 'process query failed' }
            if ($script:fake.ProcessDiesAfterPoll -gt 0 -and
                    $script:fake.PollCount -ge $script:fake.ProcessDiesAfterPoll -and
                    $script:fake.Processes.ContainsKey([int]$ProcessId)) {
                $script:fake.Processes[[int]$ProcessId].Alive = $false
            }
            if ($script:fake.Processes.ContainsKey([int]$ProcessId)) {
                return $script:fake.Processes[[int]$ProcessId]
            }
            return $null
        }
        StartTunnel = {
            param($SshExe, $ArgumentLine, $StdoutPath, $StderrPath)
            $script:fake.StartCalls++
            $script:fake.ArgumentLine = $ArgumentLine
            $script:fake.StdoutPath = $StdoutPath
            $script:fake.StderrPath = $StderrPath
            if ($script:fake.StartThrows -or
                    ($script:fake.StartThrowsOnce -and $script:fake.StartCalls -eq 1)) { throw 'start failed' }
            if ($script:fake.StartReturnsNull) { return $null }
            if ($script:fake.StartReturnsZero) { return @{ ProcessId = 0 } }
            $processId = $script:fake.NextPid
            $script:fake.NextPid++
            $commandLine = '"' + $SshExe + '" ' + $ArgumentLine
            $script:fake.Processes[$processId] = @{
                ProcessId = $processId; Name = 'ssh.exe'
                CommandLine = $commandLine; Alive = $true
            }
            if ($script:fake.StderrText) {
                [IO.File]::WriteAllText($StderrPath, $script:fake.StderrText,
                    [Text.UTF8Encoding]::new($false))
            }
            if ($script:fake.StartExits) {
                $script:fake.Processes[$processId].Alive = $false
            } elseif ($script:fake.BindOnStart) {
                $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = $processId })
            }
            return @{ ProcessId = $processId }
        }
        StopProcess = {
            param($ProcessId)
            $script:fake.StopCalls++
            $script:fake.StoppedPids.Add([int]$ProcessId)
            if ($script:fake.StopThrows) { throw 'stop failed' }
            if ($script:fake.StopExitsProcess) {
                if ($script:fake.Processes.ContainsKey([int]$ProcessId)) {
                    $script:fake.Processes[[int]$ProcessId].Alive = $false
                }
                $script:fake.Listeners = @($script:fake.Listeners |
                    Where-Object { [int]$_.OwningProcess -ne [int]$ProcessId })
            }
            if (-not $script:fake.StopSucceeds) { return $false }
            if (-not $script:fake.StopLeavesAlive -and
                    $script:fake.Processes.ContainsKey([int]$ProcessId)) {
                $script:fake.Processes[[int]$ProcessId].Alive = $false
            }
            if (-not $script:fake.StopLeavesAlive) {
                $script:fake.Listeners = @($script:fake.Listeners |
                    Where-Object { [int]$_.OwningProcess -ne [int]$ProcessId })
            }
            return $true
        }
        TestHealth = {
            param($Uri, $TimeoutSeconds)
            $script:fake.HealthCalls++
            $script:fake.HealthUris.Add([string]$Uri)
            if ($script:fake.HealthThrows) { throw 'health request failed' }
            if ($script:fake.HealthSequence.Count -gt 0) {
                $ok = [bool]$script:fake.HealthSequence[0]
                if ($script:fake.HealthSequence.Count -gt 1) {
                    $script:fake.HealthSequence = @($script:fake.HealthSequence | Select-Object -Skip 1)
                } else {
                    $script:fake.HealthSequence = @()
                }
                if ($ok) { return $script:fake.Health }
                return @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'fake not ready' }
            }
            return $script:fake.Health
        }
        IssueConsoleBootstrap = {
            param($SshExe, $ArgumentLine)
            $script:fake.BootstrapCalls++
            $script:fake.BootstrapSshExe = $SshExe
            $script:fake.BootstrapArgumentLine = $ArgumentLine
            if ($script:fake.BootstrapThrows) { throw 'ConsoleBootstrap_0123456789 exception' }
            Assert-True ($script:fake.HealthCalls -gt 0) 'bootstrap is issued after health'
            return $script:fake.BootstrapResponse
        }
        OpenBrowser = {
            param($Url)
            $script:fake.BrowserCalls++
            $script:fake.LastUrl = [string]$Url
            if ($script:fake.BrowserThrows) { throw 'browser failed' }
            return (-not $script:fake.BrowserReturnsFalse)
        }
        Sleep = {
            param($Milliseconds)
            $script:fake.SleepCalls++
        }
        EnterLock = {
            param($Name, $TimeoutMilliseconds)
            $script:fake.LockCalls++
            $script:fake.LockNames.Add([string]$Name)
            if ($script:fake.LockAvailable) { return 'fake-lock' }
            return $null
        }
        ExitLock = {
            param($Lock)
            $script:fake.UnlockCalls++
        }
        GetTailscaleStatus = {
            param($TargetAddress)
            $script:fake.TailscaleCalls++
            if ($script:fake.TailscaleThrows) { throw 'tailscale status failed' }
            return $script:fake.Tailscale
        }
        GetDesktopPath = { return $script:fake.DesktopPath }
        NewShortcutShell = { return $script:fake.Shell }
        ResolveDnsName = {
            param($Name)
            $script:fake.ResolveDnsNameCalls++
            $script:fake.ResolveDnsNameNames.Add([string]$Name)
            if ($script:fake.ResolveDnsNameThrows) { throw 'dns lookup failed' }
            return $script:fake.ResolveDnsName
        }
        TestHostsOverride = {
            param($Domain)
            $script:fake.HostsOverrideCalls++
            $script:fake.HostsOverrideDomains.Add([string]$Domain)
            if ($script:fake.HostsOverrideThrows) { throw 'hosts read failed' }
            return $script:fake.HostsOverride
        }
    }
}

function Invoke-Entry {
    param([string]$Action = 'Open', [hashtable]$Overrides = @{})
    $parameters = @{
        Action = $Action
        Adapter = $script:adapter
        SshConfig = $script:fake.ConfigPath
        SshHostAlias = 'um-share-ecs'
        PublicSshConfig = $script:fake.PublicConfigPath
        PublicSshHostAlias = 'um-share-ecs-public'
        StatePath = $script:statePath
        LogPath = $script:logPath
        HealthTimeoutSeconds = 1
        HealthPollMilliseconds = 50
        StopTimeoutSeconds = 1
        LockTimeoutMilliseconds = 100
    }
    foreach ($key in $Overrides.Keys) { $parameters[$key] = $Overrides[$key] }
    return (Invoke-CloudConsoleEntry @parameters)
}

function Set-EffectiveValue {
    param([string]$Key, $Value)
    $script:fake.EffectiveConfig[$Key] = @($Value)
}

function Set-PublicEffectiveValue {
    param([string]$Key, $Value)
    $script:fake.PublicEffectiveConfig[$Key] = @($Value)
}

function New-TestRoutes {
    return @(
        @{ Name = 'private'; ConfigPath = $script:fake.ConfigPath; HostAlias = 'um-share-ecs';
            RequireTailscaleAddress = $true },
        @{ Name = 'public'; ConfigPath = $script:fake.PublicConfigPath; HostAlias = 'um-share-ecs-public';
            RequireTailscaleAddress = $false }
    )
}

function Assert-StateExists {
    param([string]$Name = 'state file exists')
    Assert-Equal (Test-Path -LiteralPath $script:statePath -PathType Leaf) $true $Name
}

function Assert-StateAbsent {
    param([string]$Name = 'state file is absent')
    Assert-Equal (Test-Path -LiteralPath $script:statePath -PathType Leaf) $false $Name
}

function Assert-StatePid {
    param([int]$Expected, [string]$Name)
    $state = Get-Content -LiteralPath $script:statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal ([int]$state.pid) $Expected $Name
}

function Assert-StateField {
    param([string]$Field, [string]$Expected, [string]$Name)
    $state = Get-Content -LiteralPath $script:statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal ([string]$state.$Field) $Expected $Name
}

function Assert-LogContains {
    param([string]$Pattern, [string]$Name)
    $text = if (Test-Path -LiteralPath $script:logPath -PathType Leaf) {
        Get-Content -LiteralPath $script:logPath -Raw -Encoding UTF8
    } else { '' }
    Assert-Match $text $Pattern $Name
}

function Assert-LogNotContains {
    param([string]$Pattern, [string]$Name)
    $text = if (Test-Path -LiteralPath $script:logPath -PathType Leaf) {
        Get-Content -LiteralPath $script:logPath -Raw -Encoding UTF8
    } else { '' }
    Assert-NotMatch $text $Pattern $Name
}

function Assert-NoSecret {
    param([string]$Name)
    $logText = if (Test-Path -LiteralPath $script:logPath -PathType Leaf) {
        Get-Content -LiteralPath $script:logPath -Raw -Encoding UTF8
    } else { '' }
    $stateText = if (Test-Path -LiteralPath $script:statePath -PathType Leaf) {
        Get-Content -LiteralPath $script:statePath -Raw -Encoding UTF8
    } else { '' }
    Assert-True ($logText -notmatch 'SENTINEL-SECRET') "$Name log does not contain key material"
    Assert-True ($stateText -notmatch 'SENTINEL-SECRET') "$Name state does not contain key material"
}

function Assert-AtomicState {
    param([string]$Name)
    $leftovers = @(Get-ChildItem -LiteralPath (Split-Path -Parent $script:statePath) `
        -Filter '*.tmp.*' -File -ErrorAction SilentlyContinue)
    Assert-Equal $leftovers.Count 0 "$Name leaves no state temp file"
}

function Assert-PreflightFailure {
    param([scriptblock]$Mutate, [string]$Name)
    Reset-Fake
    & $Mutate
    $code = Invoke-Entry
    Assert-Equal $code 2 "$Name exits 2"
    Assert-Equal $script:fake.StartCalls 0 "$Name does not start a tunnel"
    Assert-Equal $script:fake.BrowserCalls 0 "$Name does not open a browser"
    Assert-StateAbsent "$Name leaves no state"
}

function Assert-PortFailure {
    param([scriptblock]$Setup, [string]$Name, [int]$ExpectedCode = 3)
    Reset-Fake
    & $Setup
    $code = Invoke-Entry
    Assert-Equal $code $ExpectedCode "$Name exits $ExpectedCode"
    Assert-Equal $script:fake.StartCalls 0 "$Name does not start a tunnel"
    Assert-Equal $script:fake.StopCalls 0 "$Name does not kill the listener"
    Assert-Equal $script:fake.BrowserCalls 0 "$Name does not open a browser"
}

# ---------------------------------------------------------------------------
# Static contract: files, default paths, git-ignored state and the bat wrapper.
# ---------------------------------------------------------------------------

$entryText = Get-Content -LiteralPath $entry -Raw -Encoding UTF8
$batText = Get-Content -LiteralPath $bat -Raw -Encoding UTF8
$gitignoreText = Get-Content -LiteralPath $gitignore -Raw -Encoding UTF8

Assert-Match $entryText 'CloudConsolePort = 18096' 'entry uses loopback port 18096'
Assert-Match $entryText 'CloudConsoleBind = ''127\.0\.0\.1''' 'entry binds 127.0.0.1'
Assert-Match $entryText 'http://localhost:18096/#overview' 'entry uses a browser host distinct from the default local console'
Assert-Match $entryText 'aliyun-um-ecs\.conf' 'default SSH config is the dedicated file'
Assert-Match $entryText 'um-share-ecs' 'default SSH host alias is the dedicated host'
Assert-Match $entryText 'aliyun-um-ecs-public\.conf' 'public SSH config is a dedicated file'
Assert-Match $entryText 'um-share-ecs-public' 'public SSH host alias is a dedicated host'
Assert-Match $entryText 'RequireTailscaleAddress' 'SSH preflight can require a Tailscale address'
Assert-Match $entryText 'Resolve-DnsName' 'check diagnoses the public DNS name'
Assert-Match $entryText 'dacintapi\.de5\.net' 'check diagnoses the deployed public domain'
Assert-NotMatch $entryText '100\.119\.187\.73' 'entry never hardcodes the old control IP'
Assert-NotMatch $entryText 'dacint\.tailae8db5\.ts\.net' 'entry never hardcodes the old control domain'
Assert-Match $entryText 'data\\cloud-console-tunnel\.json' 'default state file lives under data'
Assert-Match $entryText 'data\\cloud-console-entry\.log' 'default log lives under data'
Assert-Match $entryText 'cloud-console-tunnel\.stdout\.log' 'tunnel stdout log name'
Assert-Match $entryText 'cloud-console-tunnel\.stderr\.log' 'tunnel stderr log name'
Assert-Match $gitignoreText '(?m)^/ai-gateway/data/\s*$' 'gitignore covers ai-gateway/data'
Assert-Match $entryText 'BatchMode=yes' 'ssh command enforces BatchMode'
Assert-Match $entryText 'StrictHostKeyChecking=yes' 'ssh command enforces strict host checking'
Assert-Match $entryText 'IdentitiesOnly=yes' 'ssh command uses only the configured identity'
Assert-Match $entryText 'ExitOnForwardFailure=yes' 'ssh command exits on forward failure'
Assert-Match $entryText 'ServerAliveInterval=30' 'ssh command uses ServerAliveInterval 30'
Assert-Match $entryText 'ServerAliveCountMax=3' 'ssh command uses ServerAliveCountMax 3'
Assert-Match $entryText 'TCPKeepAlive=yes' 'ssh command uses TCPKeepAlive'
Assert-Match $entryText 'msedge\.exe' 'entry prefers Edge'
Assert-Match $entryText '--new-window' 'entry opens a new Edge window'
Assert-NotMatch $entryText '(?i)StrictHostKeyChecking=(no|accept-new)' 'entry never weakens host checking'
Assert-NotMatch $entryText 'Invoke-Expression' 'entry never uses Invoke-Expression'
Assert-NotMatch $entryText '(?im)Start-Process[^

]*-Wait' 'entry never blocks on a started process'
$tunnelStartText = (Get-Command Start-CloudTunnelProcess).Definition
Assert-NotMatch $tunnelStartText '(?i)WaitForExit' 'tunnel start never waits for its long-lived process'
Assert-NotMatch $entryText '(?i)tailscale\s+(up|down|login|set)' 'entry never starts or changes Tailscale'
Assert-NotMatch $entryText '(?i)(Start|Stop|Restart|Set)-Service' 'entry never starts or stops a service'
Assert-NotMatch $entryText '(?i)\bmihomo\b' 'entry never invokes Mihomo'
Assert-NotMatch $entryText '7897' 'entry never uses the local proxy port'
Assert-NotMatch $entryText 'SENTINEL-SECRET' 'entry source contains no test secret'

Assert-Match $batText 'Open-CloudConsole\.ps1' 'bat delegates to the PowerShell entry'
Assert-Match $batText '%\*' 'bat forwards lifecycle switches'
Assert-Match $batText '(?i)NO_PROXY=\*' 'bat clears proxy for loopback checks'
Assert-Match $batText '(?i)pwsh' 'bat prefers PowerShell 7'
Assert-Match $batText '(?i)powershell' 'bat has the Windows PowerShell fallback'
Assert-Match $batText 'exit /b %ERRORLEVEL%' 'bat propagates the exit code'
Assert-NotMatch $batText '(?im)^\s*ssh(?:\.exe)?\s' 'bat never starts ssh directly'
Assert-NotMatch $batText 'SENTINEL-SECRET' 'bat contains no secret'

# ---------------------------------------------------------------------------
# Argument line and command line ownership matching.
# ---------------------------------------------------------------------------

Reset-Fake
$script:adapter = New-TestAdapter

$argumentLine = Get-CloudTunnelArgumentLine -ConfigPath $script:fake.ConfigPath -HostAlias 'um-share-ecs'
Assert-Match $argumentLine '-F "' 'argument line passes the config explicitly with -F'
Assert-Match $argumentLine ([regex]::Escape($script:fake.ConfigPath)) 'argument line uses the dedicated config'
Assert-Match $argumentLine '-N' 'argument line disables the remote command'
Assert-Match $argumentLine '-L 127\.0\.0\.1:18096:127\.0\.0\.1:18096' 'argument line forwards loopback 18096'
Assert-Match $argumentLine '-o BatchMode=yes' 'argument line enforces BatchMode'
Assert-Match $argumentLine '-o StrictHostKeyChecking=yes' 'argument line enforces strict host checking'
Assert-Match $argumentLine '-o IdentitiesOnly=yes' 'argument line enforces IdentitiesOnly'
Assert-Match $argumentLine '-o ExitOnForwardFailure=yes' 'argument line enforces ExitOnForwardFailure'
Assert-Match $argumentLine '-o ConnectTimeout=15' 'argument line bounds connect timeout'
Assert-Match $argumentLine '-o ServerAliveInterval=30' 'argument line keeps alive'
Assert-Match $argumentLine '-o ServerAliveCountMax=3' 'argument line bounds keepalive'
Assert-Match $argumentLine '-o TCPKeepAlive=yes' 'argument line enables TCP keepalive'
Assert-NotMatch $argumentLine '(?i)StrictHostKeyChecking=(no|accept-new)' 'argument line never weakens host checking'
Assert-NotMatch $argumentLine '(?:^|\s)-i(?:\s|$)' 'argument line does not pass a second identity'

$canonical = New-FakeTunnelCommandLine
Assert-True (Test-CloudTunnelCommandLine -CommandLine $canonical -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private') 'canonical tunnel command line is owned'
$publicCanonical = New-FakeTunnelCommandLine -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public'
Assert-True (Test-CloudTunnelCommandLine -CommandLine $publicCanonical -ConfigPath $script:fake.PublicConfigPath `
    -HostAlias 'um-share-ecs-public' -Route 'public') 'canonical public tunnel command line is owned for the public route'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $publicCanonical -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'a public tunnel command line is not owned for the private route'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $canonical -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'bogus')) 'an unknown route is never owned'
$bareForward = '"C:\fake\ssh.exe" -F "' + $script:fake.ConfigPath + '" -N -L 18096:127.0.0.1:18096 um-share-ecs'
Assert-True (Test-CloudTunnelCommandLine -CommandLine $bareForward -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private') 'bare loopback forward is owned'
$noSpace = '"C:\fake\ssh.exe" -F"' + $script:fake.ConfigPath + '" -N -L127.0.0.1:18096:127.0.0.1:18096 um-share-ecs'
Assert-True (Test-CloudTunnelCommandLine -CommandLine $noSpace -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private') 'compact -F/-L forms are owned'
$wrongHost = '"C:\fake\ssh.exe" -F "' + $script:fake.ConfigPath + '" -N -L 127.0.0.1:18096:127.0.0.1:18096 other-host'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $wrongHost -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'a different host alias is not owned'
$wrongForward = '"C:\fake\ssh.exe" -F "' + $script:fake.ConfigPath + '" -N -L 127.0.0.1:18096:127.0.0.1:8001 um-share-ecs'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $wrongForward -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'a forward to 8001 is not owned'
$noN = '"C:\fake\ssh.exe" -F "' + $script:fake.ConfigPath + '" -L 18096:127.0.0.1:18096 um-share-ecs'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $noN -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'an interactive ssh session is not owned'
$noF = '"C:\fake\ssh.exe" -N -L 18096:127.0.0.1:18096 um-share-ecs'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine $noF -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'a tunnel without -F is not owned'
Assert-True (-not (Test-CloudTunnelCommandLine -CommandLine '' -ConfigPath $script:fake.ConfigPath `
    -HostAlias 'um-share-ecs' -Route 'private')) 'an empty command line is not owned'

Assert-True (Test-CloudTailscaleAddress '100.71.250.7') 'Tailscale CGNAT address is accepted'
Assert-True (-not (Test-CloudTailscaleAddress '100.63.255.255')) 'address below CGNAT is rejected'
Assert-True (-not (Test-CloudTailscaleAddress '203.0.113.10')) 'public address is rejected on the private route'
Assert-True (-not (Test-CloudTailscaleAddress '100.71.250.7.example')) 'host name is rejected'
Assert-True (Test-CloudPublicHost '203.0.113.10') 'public IPv4 address is accepted'
Assert-True (Test-CloudPublicHost '2001:db8::10') 'public IPv6 address is accepted'
Assert-True (Test-CloudPublicHost 'ecs.example.com') 'public DNS name is accepted'
Assert-True (-not (Test-CloudPublicHost '')) 'empty public host is rejected'
Assert-True (-not (Test-CloudPublicHost 'bad host')) 'public host with a space is rejected'
Assert-True (-not (Test-CloudPublicHost '-bad.example.com')) 'public host with a leading hyphen is rejected'
Assert-True (-not (Test-CloudPublicHost 'bad_.example.com')) 'public host with an underscore is rejected'

# ---------------------------------------------------------------------------
# A4 cold start: no listener, no state -> start one tunnel, health, Edge, state.
# ---------------------------------------------------------------------------

Reset-Fake
$code = Invoke-Entry
Assert-Equal $code 0 'cold start exits 0'
Assert-Equal $script:fake.StartCalls 1 'cold start starts one tunnel'
Assert-Equal $script:fake.BrowserCalls 1 'cold start opens one browser'
Assert-Equal $script:fake.LastUrl 'http://localhost:18096/display/grant#console=ConsoleBootstrap_0123456789' 'cold start opens the bootstrap URL on isolated browser host'
Assert-Equal $script:fake.StopCalls 0 'cold start does not stop anything'
Assert-Equal $script:fake.HealthCalls 1 'cold start performs one health check'
Assert-Equal $script:fake.HealthUris[0] 'http://127.0.0.1:18096/health' 'health check targets loopback 18096'
Assert-Equal $script:fake.EffectiveCalls 1 'cold start validates the SSH config once'
Assert-Equal $script:fake.TailscaleCalls 1 'cold start checks Tailscale once'
Assert-Equal $script:fake.LockCalls 1 'cold start takes the entry lock'
Assert-Equal $script:fake.UnlockCalls 1 'cold start releases the entry lock'
Assert-Equal $script:fake.LockNames[0] 'Local\UMCloudConsoleEntry' 'entry lock name is fixed'
Assert-Match $script:fake.ArgumentLine '-L 127\.0\.0\.1:18096:127\.0\.0\.1:18096' 'started ssh forwards loopback 18096'
Assert-Match $script:fake.ArgumentLine ([regex]::Escape($script:fake.ConfigPath)) 'started ssh uses the dedicated config'
Assert-Equal $script:fake.StdoutPath (Join-Path (Split-Path -Parent $script:statePath) 'cloud-console-tunnel.stdout.log') 'stdout log stays next to state'
Assert-Equal $script:fake.StderrPath (Join-Path (Split-Path -Parent $script:statePath) 'cloud-console-tunnel.stderr.log') 'stderr log stays next to state'
Assert-StateExists 'cold start writes the state file'
Assert-StatePid 4100 'state records the started pid'
Assert-StateField 'bind' '127.0.0.1' 'state records loopback bind'
Assert-StateField 'port' '18096' 'state records port 18096'
Assert-StateField 'target' '127.0.0.1:18096' 'state records the forward target'
Assert-StateField 'ssh_host' 'um-share-ecs' 'state records the host alias'
Assert-LogContains 'open\.started' 'cold start logs the started event'
Assert-LogContains 'open\.ready' 'cold start logs the ready event'
Assert-AtomicState 'cold start'
Assert-NoSecret 'cold start'
Assert-StateField 'route' 'private' 'cold start records the private route'
Assert-True ($script:fake.ArgumentLine.Contains('um-share-ecs')) 'cold start uses the private host alias'
Assert-True ($script:fake.BootstrapArgumentLine.Contains('um-share-ecs')) 'cold start authorizes over the private alias'

# ---------------------------------------------------------------------------
# Route selection: private first, public fallback, both routes reported.
# ---------------------------------------------------------------------------

Reset-Fake
$code = Invoke-Entry
Assert-Equal $code 0 'private route success exits 0'
Assert-Equal $script:fake.StartCalls 1 'private route success starts one tunnel'
Assert-Match $script:fake.ArgumentLine ([regex]::Escape($script:fake.ConfigPath)) 'private route success uses the private config'
Assert-NotMatch $script:fake.ArgumentLine ([regex]::Escape($script:fake.PublicConfigPath)) 'private route success never uses the public config'
Assert-Equal $script:fake.TailscaleCalls 1 'private route success checks Tailscale'
Assert-Equal $script:fake.EffectiveCalls 1 'private route success validates only the private config'
Assert-Equal $script:fake.PublicEffectiveCalls 0 'private route success never preflights the public config'
Assert-StateField 'route' 'private' 'private route records the private route'
Assert-True ($script:fake.BootstrapArgumentLine.Contains($script:fake.ConfigPath)) 'private route authorizes over the private config'
Assert-True ($script:fake.BootstrapArgumentLine.Contains('um-share-ecs')) 'private route authorizes over the private alias'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$code = Invoke-Entry
Assert-Equal $code 0 'private preflight failure falls back to public and exits 0'
Assert-Equal $script:fake.StartCalls 1 'fallback starts one public tunnel'
Assert-Match $script:fake.ArgumentLine ([regex]::Escape($script:fake.PublicConfigPath)) 'fallback starts ssh with the public config'
Assert-Match $script:fake.ArgumentLine 'um-share-ecs-public' 'fallback uses the public host alias'
Assert-Equal $script:fake.TailscaleCalls 0 'public route never queries Tailscale'
Assert-StateField 'route' 'public' 'fallback records the public route'
Assert-StateField 'ssh_config' $script:fake.PublicConfigPath 'fallback records the public config'
Assert-StateField 'ssh_host' 'um-share-ecs-public' 'fallback records the public host alias'
Assert-True ($script:fake.BootstrapArgumentLine.Contains($script:fake.PublicConfigPath)) 'fallback authorizes over the public config'
Assert-True ($script:fake.BootstrapArgumentLine.Contains('um-share-ecs-public')) 'fallback authorizes over the public alias'
Assert-Equal $script:fake.BrowserCalls 1 'fallback opens the page'
Assert-LogContains 'open\.fallback' 'fallback is logged'

Reset-Fake
$script:fake.PublicConfigExists = $true
$script:fake.StartThrowsOnce = $true
$code = Invoke-Entry
Assert-Equal $code 0 'private start failure falls back to public and exits 0'
Assert-Equal $script:fake.StartCalls 2 'private start failure starts a public tunnel after the private attempt'
Assert-Match $script:fake.ArgumentLine ([regex]::Escape($script:fake.PublicConfigPath)) 'start fallback uses the public config'
Assert-StateField 'route' 'public' 'start fallback records the public route'
Assert-LogContains 'open\.startfailed' 'start fallback logs the private start failure'
Assert-LogContains 'open\.fallback' 'start fallback logs the route switch'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
Set-PublicEffectiveValue 'stricthostkeychecking' @('no')
$captured = @(Invoke-Entry 6>&1)
$code = $captured[-1]
$text = ($captured[0..($captured.Count - 2)] -join ' ')
Assert-Equal $code 2 'both routes failing exits 2'
Assert-Match $text 'private:' 'both routes failure names the private reason'
Assert-Match $text 'public:' 'both routes failure names the public reason'
Assert-Equal $script:fake.StartCalls 0 'both routes failing starts no tunnel'
Assert-Equal $script:fake.BrowserCalls 0 'both routes failing opens no page'
Assert-StateAbsent 'both routes failing leaves no state'
Assert-LogContains 'open\.failed' 'both routes failing is logged'

# ---------------------------------------------------------------------------
# A4 restart: stale state, corrupt state and pid reuse all start exactly once.
# ---------------------------------------------------------------------------

Reset-Fake
Write-FakeState -ProcessId 4000
$code = Invoke-Entry
Assert-Equal $code 0 'restart with a dead state pid exits 0'
Assert-Equal $script:fake.StartCalls 1 'restart with a dead state pid starts one tunnel'
Assert-Equal $script:fake.BrowserCalls 1 'restart opens one browser'
Assert-StatePid 4100 'restart replaces the stale state pid'
Assert-LogContains 'open\.stale' 'restart logs the stale state cleanup'

Reset-Fake
[IO.File]::WriteAllText($script:statePath, '{not-json',
    [Text.UTF8Encoding]::new($false))
$code = Invoke-Entry
Assert-Equal $code 0 'restart with corrupt state exits 0'
Assert-Equal $script:fake.StartCalls 1 'restart with corrupt state starts one tunnel'
Assert-StatePid 4100 'restart replaces the corrupt state'
Assert-LogContains 'open\.stale' 'restart logs the corrupt state cleanup'

Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine -HostAlias 'other-host')
}
$code = Invoke-Entry
Assert-Equal $code 0 'restart with a reused pid exits 0'
Assert-Equal $script:fake.StartCalls 1 'restart with a reused pid starts one tunnel'
Assert-StatePid 4100 'restart replaces the reused pid'
Assert-LogContains 'open\.stale' 'restart logs the reused pid cleanup'

# ---------------------------------------------------------------------------
# A4 consecutive clicks and closing the page.
# ---------------------------------------------------------------------------

Reset-Fake
$first = Invoke-Entry
$second = Invoke-Entry
Assert-Equal $first 0 'first click exits 0'
Assert-Equal $second 0 'second click exits 0'
Assert-Equal $script:fake.StartCalls 1 'consecutive clicks start only one tunnel'
Assert-Equal $script:fake.BrowserCalls 2 'consecutive clicks open the page twice'
Assert-Equal $script:fake.StopCalls 0 'consecutive clicks stop nothing'
Assert-StateExists 'consecutive clicks keep the state file'
Assert-LogContains 'open\.reuse' 'consecutive clicks reuse the healthy tunnel'

Reset-Fake
$opened = Invoke-Entry
Assert-Equal $opened 0 'page open exits 0'
Assert-Equal $script:fake.StopCalls 0 'closing the page does not stop the tunnel'
Assert-StateExists 'closing the page leaves the state file'
$checkCode = Invoke-Entry -Action Check
Assert-Equal $checkCode 0 'check after closing the page reports healthy'
Assert-Equal $script:fake.StartCalls 1 'check after closing the page starts nothing'
Assert-Equal $script:fake.StopCalls 0 'check after closing the page stops nothing'
Assert-StateExists 'check after closing the page leaves the state file'

# Reuse a healthy tunnel with no state file (adoption).
Reset-Fake
Add-OwnTunnel -ProcessId 4200
$code = Invoke-Entry
Assert-Equal $code 0 'adopting a healthy tunnel exits 0'
Assert-Equal $script:fake.StartCalls 0 'adopting a healthy tunnel starts nothing'
Assert-Equal $script:fake.BrowserCalls 1 'adopting a healthy tunnel opens the page'
Assert-Equal $script:fake.EffectiveCalls 1 'adopting validates SSH before authorization'
Assert-Equal $script:fake.TailscaleCalls 0 'adopting a healthy tunnel skips Tailscale'
Assert-StatePid 4200 'adopting records the listener pid'
Assert-LogContains 'open\.reuse' 'adopting logs the reuse event'

# Authorization still requires dedicated SSH preflight on a reused tunnel.
Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.SshMissing = $true
$script:fake.ConfigExists = $false
$code = Invoke-Entry
Assert-Equal $code 2 'reuse rejects a missing ssh/config'
Assert-Equal $script:fake.StartCalls 0 'reuse with a missing ssh starts nothing'
Assert-Equal $script:fake.BrowserCalls 0 'reuse with a missing ssh opens no page'

# ---------------------------------------------------------------------------
# A4 network recovery: an owned tunnel stays put; a recovered tunnel is reused.
# ---------------------------------------------------------------------------

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'connection refused' }
$first = Invoke-Entry
Assert-Equal $first 4 'unhealthy owned tunnel exits 4'
Assert-Equal $script:fake.StartCalls 0 'unhealthy owned tunnel starts nothing'
Assert-Equal $script:fake.StopCalls 0 'unhealthy owned tunnel kills nothing'
Assert-Equal $script:fake.BrowserCalls 0 'unhealthy owned tunnel opens no page'
Assert-StateExists 'unhealthy owned tunnel keeps the state file'
Assert-LogContains 'open\.timeout' 'unhealthy owned tunnel logs the failure'

$script:fake.Health = @{ Ok = $true; StatusCode = 200;
    Content = '{"status":"ok","service":"usagesrv","port":18096}'; Detail = '' }
$second = Invoke-Entry
Assert-Equal $second 0 'recovered tunnel exits 0'
Assert-Equal $script:fake.StartCalls 0 'recovered tunnel is reused, not restarted'
Assert-Equal $script:fake.BrowserCalls 1 'recovered tunnel opens the page'
Assert-StateExists 'recovered tunnel keeps the state file'

# A tunnel process that is alive but has not bound the port yet is adopted and
# waited for, instead of starting a duplicate.
Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
$script:fake.BindAfterPoll = 2
$script:fake.ListenerWhenBound = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 4200 })
$code = Invoke-Entry
Assert-Equal $code 0 'a starting tunnel is adopted and waited for'
Assert-Equal $script:fake.StartCalls 0 'a starting tunnel is not duplicated'
Assert-Equal $script:fake.BrowserCalls 1 'a starting tunnel opens the page after binding'
Assert-StatePid 4200 'a starting tunnel keeps its pid'
Assert-LogContains 'open\.reuse' 'a starting tunnel logs reuse'

# A tunnel process that exits during the wait removes its state and reports the
# actionable failure.
Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
$script:fake.ProcessDiesAfterPoll = 2
$code = Invoke-Entry
Assert-Equal $code 1 'a tunnel exiting during the wait exits 1'
Assert-Equal $script:fake.StartCalls 0 'a tunnel exiting during the wait starts nothing'
Assert-StateAbsent 'a tunnel exiting during the wait removes the state'
Assert-LogContains 'open\.exited' 'a tunnel exiting during the wait logs the exit'

# A tunnel that stays alive but never becomes healthy keeps its state for -Stop.
Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
$script:fake.BindAfterPoll = 2
$script:fake.ListenerWhenBound = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 4200 })
$script:fake.Health = @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'connection refused' }
$code = Invoke-Entry
Assert-Equal $code 4 'a starting tunnel that never becomes healthy exits 4'
Assert-Equal $script:fake.StartCalls 0 'a starting tunnel that never becomes healthy starts nothing'
Assert-StateExists 'a starting tunnel that never becomes healthy keeps the state'
Assert-LogContains 'open\.timeout' 'a starting tunnel that never becomes healthy logs timeout'

# ---------------------------------------------------------------------------
# A4 port ownership: foreign, non-loopback and unconfirmed listeners are refused
# and never killed.
# ---------------------------------------------------------------------------

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
    }
} 'a foreign loopback listener'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '0.0.0.0'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'ssh.exe'; CommandLine = (New-FakeTunnelCommandLine); Alive = $true
    }
} 'a wildcard listener'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '::1'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'ssh.exe'; CommandLine = (New-FakeTunnelCommandLine); Alive = $true
    }
} 'an IPv6 loopback listener'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = 'localhost'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'ssh.exe'; CommandLine = (New-FakeTunnelCommandLine); Alive = $true
    }
} 'an unparsable listener address'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 0 })
} 'a listener without an owning pid'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
} 'a listener whose process is gone'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'ssh.exe'; CommandLine = ''; Alive = $true
    }
} 'an ssh listener without a readable command line'

Assert-PortFailure {
    Add-OwnTunnel -ProcessId 999 -HostAlias 'other-host'
} 'an ssh tunnel to a different host'

Assert-PortFailure {
    $script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'plink.exe'; CommandLine = 'plink.exe'; Alive = $true
    }
} 'a non-ssh listener'

Assert-PortFailure {
    $script:fake.ListenerThrows = $true
} 'an unreadable listener query'

Assert-PortFailure {
    Add-OwnTunnel -ProcessId 4200
    $script:fake.Listeners = @(
        @{ LocalAddress = '127.0.0.1'; OwningProcess = 4200 },
        @{ LocalAddress = '127.0.0.1'; OwningProcess = 4300 }
    )
    $script:fake.Processes[4300] = @{
        ProcessId = 4300; Name = 'ssh.exe'; CommandLine = (New-FakeTunnelCommandLine); Alive = $true
    }
} 'multiple matching listeners'

Assert-PortFailure {
    Add-OwnTunnel -ProcessId 4200
    $script:fake.Listeners = @(
        @{ LocalAddress = '127.0.0.1'; OwningProcess = 4200 },
        @{ LocalAddress = '127.0.0.1'; OwningProcess = 999 }
    )
    $script:fake.Processes[999] = @{
        ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
    }
} 'one owned and one foreign listener'

# The IPv4-mapped loopback form still identifies the same listener.
Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Listeners = @(@{ LocalAddress = '::ffff:127.0.0.1'; OwningProcess = 4200 })
$code = Invoke-Entry
Assert-Equal $code 0 'an IPv4-mapped loopback listener is reused'
Assert-Equal $script:fake.StartCalls 0 'an IPv4-mapped loopback listener starts nothing'

# ---------------------------------------------------------------------------
# Public route ownership: only the current public tunnel is reused.
# ---------------------------------------------------------------------------

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
$script:fake.Processes[999] = @{
    ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
}
$code = Invoke-Entry
Assert-Equal $code 3 'public route refuses a foreign listener'
Assert-Equal $script:fake.StartCalls 0 'public route does not start over a foreign listener'
Assert-Equal $script:fake.StopCalls 0 'public route does not kill a foreign listener'
Assert-Equal $script:fake.BrowserCalls 0 'public route does not open a page over a foreign listener'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$oldPublicConfig = Join-Path $script:tempRoot 'aliyun-um-ecs-public-old.conf'
$script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 4400 })
$script:fake.Processes[4400] = @{
    ProcessId = 4400; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine -ConfigPath $oldPublicConfig -HostAlias 'um-share-ecs-public')
}
$code = Invoke-Entry
Assert-Equal $code 3 'an old public tunnel is not reused'
Assert-Equal $script:fake.StartCalls 0 'an old public tunnel blocks a new public tunnel'
Assert-Equal $script:fake.StopCalls 0 'an old public tunnel is not killed'
Assert-Equal $script:fake.BrowserCalls 0 'an old public tunnel opens no page'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
Add-OwnTunnel -ProcessId 4300 -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public'
$code = Invoke-Entry
Assert-Equal $code 0 'a healthy public tunnel is reused'
Assert-Equal $script:fake.StartCalls 0 'a healthy public tunnel is not restarted'
Assert-Equal $script:fake.BrowserCalls 1 'a healthy public tunnel opens the page'
Assert-Equal $script:fake.TailscaleCalls 0 'a healthy public tunnel never queries Tailscale'
Assert-Equal $script:fake.PublicEffectiveCalls 1 'a healthy public tunnel validates the public config'
Assert-StateField 'route' 'public' 'a healthy public tunnel records the public route'
Assert-True ($script:fake.BootstrapArgumentLine.Contains($script:fake.PublicConfigPath)) 'a healthy public tunnel authorizes over the public config'

# Legacy state files written before the route field still resolve by config/host.
Reset-Fake
$legacyRoutes = New-TestRoutes
$legacyPrivate = @{ pid = 4200; ssh_config = $script:fake.ConfigPath; ssh_host = 'um-share-ecs' }
$legacyPublic = @{ pid = 4200; ssh_config = $script:fake.PublicConfigPath; ssh_host = 'um-share-ecs-public' }
$legacyUnknown = @{ pid = 4200; route = 'bogus'; ssh_config = $script:fake.ConfigPath; ssh_host = 'um-share-ecs' }
$legacyInconsistent = @{ pid = 4200; route = 'public'; ssh_config = $script:fake.ConfigPath; ssh_host = 'um-share-ecs' }
Assert-Equal (Get-CloudConsoleStateRoute -State $legacyPrivate -Routes $legacyRoutes) 'private' 'legacy private state infers the private route'
Assert-Equal (Get-CloudConsoleStateRoute -State $legacyPublic -Routes $legacyRoutes) 'public' 'legacy public state infers the public route'
Assert-Equal (Get-CloudConsoleStateRoute -State $legacyUnknown -Routes $legacyRoutes) '' 'an invalid route field is never inferred'
Assert-Equal (Get-CloudConsoleStateRoute -State $legacyInconsistent -Routes $legacyRoutes) '' 'a route field inconsistent with config is never trusted'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$script:fake.Health = @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'connection refused' }
$code = Invoke-Entry
Assert-Equal $code 4 'a public tunnel that never becomes healthy exits 4'
Assert-Equal $script:fake.StartCalls 1 'a public tunnel that never becomes healthy starts once'
Assert-Equal $script:fake.StopCalls 0 'a public tunnel that never becomes healthy keeps its process'
Assert-StateField 'route' 'public' 'a public tunnel that never becomes healthy keeps the public route'
Assert-LogContains 'open\.timeout' 'a public tunnel that never becomes healthy logs the timeout'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$script:fake.StartExits = $true
$script:fake.StderrText = 'Connection timed out'
$captured = @(Invoke-Entry 6>&1)
$code = $captured[-1]
$text = ($captured[0..($captured.Count - 2)] -join ' ')
Assert-Equal $code 1 'a public tunnel that exits during the wait exits 1'
Assert-Match $text '公网' 'public tunnel failure names the public route'
Assert-Match $text '安全组' 'public tunnel failure points at the public network boundary'
Assert-LogContains 'open\.exited' 'a public tunnel that exits during the wait logs the exit'

# A live state process for the other route must not be duplicated while the
# selected route has no listener yet.
Reset-Fake
Write-FakeState -ProcessId 4200 -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public' -Route 'public'
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public')
}
$code = Invoke-Entry
Assert-Equal $code 4 'a live other-route state without a listener is not duplicated'
Assert-Equal $script:fake.StartCalls 0 'a live other-route state starts no new tunnel'
Assert-Equal $script:fake.StopCalls 0 'a live other-route state kills nothing'
Assert-StateExists 'a live other-route state keeps its state file'
Assert-LogContains 'open\.unhealthy' 'a live other-route state is logged'

# ---------------------------------------------------------------------------
# Health contract: wrong service, wrong port and transient failures never pass.
# ---------------------------------------------------------------------------

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $true; StatusCode = 200;
    Content = '{"status":"ok","service":"usagesrv","port":8001}'; Detail = '' }
$code = Invoke-Entry
Assert-Equal $code 4 'a health reply from port 8001 is rejected'
Assert-Equal $script:fake.BrowserCalls 0 'a health reply from port 8001 opens no page'
Assert-StateExists 'a health reply from port 8001 keeps the tunnel'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $true; StatusCode = 200;
    Content = '{"status":"ok","service":"other","port":18096}'; Detail = '' }
$code = Invoke-Entry
Assert-Equal $code 4 'a health reply from another service is rejected'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $true; StatusCode = 503; Content = '{}'; Detail = '' }
$code = Invoke-Entry
Assert-Equal $code 4 'a non-200 health reply is rejected'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $true; StatusCode = 200; Content = 'not-json'; Detail = '' }
$code = Invoke-Entry
Assert-Equal $code 4 'a malformed health reply is rejected'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.HealthThrows = $true
$code = Invoke-Entry
Assert-Equal $code 4 'a throwing health request is rejected'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.HealthSequence = @($false, $true)
$code = Invoke-Entry
Assert-Equal $code 0 'a transient health failure recovers on the next poll'
Assert-Equal $script:fake.HealthCalls 2 'a transient health failure is retried once'
Assert-Equal $script:fake.BrowserCalls 1 'a recovered health check opens the page'

# ---------------------------------------------------------------------------
# Preflight: SSH config, original key, known_hosts and Tailscale failures.
# ---------------------------------------------------------------------------

Assert-PreflightFailure { $script:fake.ConfigExists = $false } 'a missing SSH config'
Assert-PreflightFailure { $script:fake.SshMissing = $true } 'a missing ssh.exe'
Assert-PreflightFailure { $script:fake.EffectiveThrows = $true } 'a failing ssh -G'
Assert-PreflightFailure { $script:fake.IdentityExists = $false } 'a missing original key'
Assert-PreflightFailure { $script:fake.KnownHostsExists = $false } 'a missing known_hosts file'
Assert-PreflightFailure { Set-EffectiveValue 'batchmode' @('no') } 'BatchMode disabled'
Assert-PreflightFailure { Set-EffectiveValue 'stricthostkeychecking' @('accept-new') } 'StrictHostKeyChecking accept-new'
Assert-PreflightFailure { Set-EffectiveValue 'identitiesonly' @('no') } 'IdentitiesOnly disabled'
Assert-PreflightFailure { Set-EffectiveValue 'tcpkeepalive' @('no') } 'TCPKeepAlive disabled'
Assert-PreflightFailure { Set-EffectiveValue 'hostname' @('203.0.113.10') } 'a public host name'
Assert-PreflightFailure { Set-EffectiveValue 'user' @('') } 'a missing user'
Assert-PreflightFailure { Set-EffectiveValue 'port' @('0') } 'an invalid port'
Assert-PreflightFailure { Set-EffectiveValue 'connecttimeout' @('0') } 'a zero connect timeout'
Assert-PreflightFailure { Set-EffectiveValue 'serveraliveinterval' @('0') } 'a zero server alive interval'
Assert-PreflightFailure { Set-EffectiveValue 'serveralivecountmax' @('0') } 'a zero server alive count'
Assert-PreflightFailure { Set-EffectiveValue 'identityfile' @() } 'a missing identity file entry'
Assert-PreflightFailure { Set-EffectiveValue 'userknownhostsfile' @() } 'a missing known_hosts entry'

# The public route keeps every strict SSH check except the Tailscale CGNAT host
# requirement, and its host must still be a valid DNS name or IP.
foreach ($case in @(
    @{ Name = 'BatchMode disabled'; Key = 'batchmode'; Value = @('no') },
    @{ Name = 'StrictHostKeyChecking accept-new'; Key = 'stricthostkeychecking'; Value = @('accept-new') },
    @{ Name = 'IdentitiesOnly disabled'; Key = 'identitiesonly'; Value = @('no') },
    @{ Name = 'TCPKeepAlive disabled'; Key = 'tcpkeepalive'; Value = @('no') },
    @{ Name = 'missing identity entry'; Key = 'identityfile'; Value = @() },
    @{ Name = 'missing known_hosts entry'; Key = 'userknownhostsfile'; Value = @() },
    @{ Name = 'missing user'; Key = 'user'; Value = @('') },
    @{ Name = 'invalid port'; Key = 'port'; Value = @('0') },
    @{ Name = 'zero connect timeout'; Key = 'connecttimeout'; Value = @('0') },
    @{ Name = 'zero server alive interval'; Key = 'serveraliveinterval'; Value = @('0') },
    @{ Name = 'zero server alive count'; Key = 'serveralivecountmax'; Value = @('0') },
    @{ Name = 'invalid public host'; Key = 'hostname'; Value = @('bad host') }
)) {
    Assert-PreflightFailure {
        $script:fake.ConfigExists = $false
        $script:fake.PublicConfigExists = $true
        Set-PublicEffectiveValue $case.Key $case.Value
    } "public route rejects $($case.Name)"
}

Assert-PreflightFailure {
    $script:fake.ConfigExists = $false
    $script:fake.PublicConfigExists = $true
    $script:fake.PublicIdentityExists = $false
} 'public route rejects a missing identity file'
Assert-PreflightFailure {
    $script:fake.ConfigExists = $false
    $script:fake.PublicConfigExists = $true
    $script:fake.PublicKnownHostsExists = $false
} 'public route rejects a missing known_hosts file'
Assert-PreflightFailure {
    $script:fake.ConfigExists = $false
    $script:fake.PublicConfigExists = $true
    $script:fake.PublicEffectiveThrows = $true
} 'public route rejects a failing ssh -G'

# Tailscale states that cannot work fail before starting ssh.
Assert-PreflightFailure {
    $script:fake.Tailscale = @{ Available = $true; BackendState = 'Stopped'; SelfOnline = $true;
        PeerFound = $true; PeerOnline = $true; Detail = '' }
} 'Tailscale stopped'
Assert-PreflightFailure {
    $script:fake.Tailscale = @{ Available = $true; BackendState = 'Running'; SelfOnline = $false;
        PeerFound = $true; PeerOnline = $true; Detail = '' }
} 'this host offline in Tailscale'
Assert-PreflightFailure {
    $script:fake.Tailscale = @{ Available = $true; BackendState = 'Running'; SelfOnline = $true;
        PeerFound = $true; PeerOnline = $false; Detail = '' }
} 'the cloud host offline in Tailscale'

# A missing Tailscale CLI or an unreadable status only warns and continues.
Reset-Fake
$script:fake.Tailscale = @{ Available = $false; Detail = 'tailscale CLI 不在 PATH' }
$code = Invoke-Entry
Assert-Equal $code 0 'a missing tailscale CLI still opens the tunnel'
Assert-Equal $script:fake.StartCalls 1 'a missing tailscale CLI still starts ssh'
Assert-Equal $script:fake.BrowserCalls 1 'a missing tailscale CLI still opens the page'
Assert-LogContains 'open\.tailscale' 'a missing tailscale CLI is logged'

Reset-Fake
$script:fake.Tailscale = @{ Available = $true; BackendState = 'Running'; SelfOnline = $true;
    PeerFound = $false; PeerOnline = $false; Detail = '' }
$code = Invoke-Entry
Assert-Equal $code 0 'an unknown Tailscale peer still tries ssh'
Assert-Equal $script:fake.StartCalls 1 'an unknown Tailscale peer starts ssh'

Reset-Fake
$script:fake.TailscaleThrows = $true
$code = Invoke-Entry
Assert-Equal $code 0 'a throwing tailscale status still tries ssh'
Assert-Equal $script:fake.StartCalls 1 'a throwing tailscale status starts ssh'
Assert-LogContains 'open\.tailscale' 'a throwing tailscale status is logged'

# ---------------------------------------------------------------------------
# Start and browser failures: state is never silently left inconsistent.
# ---------------------------------------------------------------------------

Reset-Fake
$script:fake.StartThrows = $true
$code = Invoke-Entry
Assert-Equal $code 1 'a throwing StartTunnel exits 1'
Assert-Equal $script:fake.StartCalls 1 'a throwing StartTunnel was attempted once'
Assert-Equal $script:fake.BrowserCalls 0 'a throwing StartTunnel opens no page'
Assert-StateAbsent 'a throwing StartTunnel leaves no state'
Assert-LogContains 'open\.startfailed' 'a throwing StartTunnel is logged'

foreach ($flag in @('StartReturnsNull', 'StartReturnsZero')) {
    Reset-Fake
    $script:fake[$flag] = $true
    $code = Invoke-Entry
    Assert-Equal $code 1 "$flag exits 1"
    Assert-Equal $script:fake.StartCalls 1 "$flag starts once"
    Assert-StateAbsent "$flag leaves no state"
}

Reset-Fake
$script:fake.StartExits = $true
$script:fake.StderrText = 'Permission denied (publickey)'
$code = Invoke-Entry
Assert-Equal $code 1 'an exiting ssh tunnel exits 1'
Assert-Equal $script:fake.BrowserCalls 0 'an exiting ssh tunnel opens no page'
Assert-StateAbsent 'an exiting ssh tunnel leaves no state'
Assert-LogContains 'open\.exited' 'an exiting ssh tunnel logs the exit'
Assert-LogContains '认证失败' 'an exiting ssh tunnel reports the auth hint'

Reset-Fake
$script:fake.BrowserThrows = $true
$code = Invoke-Entry
Assert-Equal $code 1 'a browser failure exits 1'
Assert-Equal $script:fake.StartCalls 1 'a browser failure keeps the started tunnel'
Assert-Equal $script:fake.StopCalls 0 'a browser failure does not stop the tunnel'
Assert-StateExists 'a browser failure keeps the state file'
Assert-LogContains 'open\.browserfailed' 'a browser failure is logged'

# ---------------------------------------------------------------------------
# Lock: concurrent clicks never start a second tunnel.
# ---------------------------------------------------------------------------

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.LockAvailable = $false
$code = Invoke-Entry
Assert-Equal $code 0 'a locked entry reuses a healthy tunnel'
Assert-Equal $script:fake.StartCalls 0 'a locked entry starts nothing'
Assert-Equal $script:fake.BrowserCalls 1 'a locked entry opens the page'
Assert-Equal $script:fake.UnlockCalls 0 'a locked entry releases nothing'

Reset-Fake
$script:fake.LockAvailable = $false
$code = Invoke-Entry
Assert-Equal $code 1 'a locked entry without a tunnel exits 1'
Assert-Equal $script:fake.StartCalls 0 'a locked entry without a tunnel starts nothing'
Assert-LogContains 'open\.locked' 'a locked entry without a tunnel is logged'

Reset-Fake
$script:fake.LockAvailable = $false
$code = Invoke-Entry -Action Stop
Assert-Equal $code 1 'a locked stop exits 1'
Assert-Equal $script:fake.StopCalls 0 'a locked stop kills nothing'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.LockAvailable = $false
$script:fake.Health = @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'connection refused' }
$code = Invoke-Entry
Assert-Equal $code 4 'a locked entry reports an unhealthy tunnel'
Assert-Equal $script:fake.StartCalls 0 'a locked entry does not start another tunnel'
Assert-Equal $script:fake.BrowserCalls 0 'a locked entry does not open an unhealthy page'
Assert-LogContains 'open\.unhealthy' 'a locked entry logs the unhealthy tunnel'

# ---------------------------------------------------------------------------
# -Check lifecycle: read-only, no start/stop/browser.
# ---------------------------------------------------------------------------

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$code = Invoke-Entry -Action Check
Assert-Equal $code 0 'check of a healthy tunnel exits 0'
Assert-Equal $script:fake.StartCalls 0 'check starts nothing'
Assert-Equal $script:fake.StopCalls 0 'check stops nothing'
Assert-Equal $script:fake.BrowserCalls 0 'check opens no page'
Assert-Equal $script:fake.LockCalls 0 'check takes no lock'
Assert-Equal $script:fake.TailscaleCalls 0 'check does not query Tailscale'
Assert-LogContains 'check\.healthy' 'check logs healthy'

Reset-Fake
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check without a tunnel exits 1'
Assert-Equal $script:fake.StartCalls 0 'check without a tunnel starts nothing'
Assert-Equal $script:fake.StopCalls 0 'check without a tunnel stops nothing'
Assert-LogContains 'check\.absent' 'check without a tunnel logs absent'

Reset-Fake
$script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
$script:fake.Processes[999] = @{
    ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
}
$code = Invoke-Entry -Action Check
Assert-Equal $code 3 'check of a foreign listener exits 3'
Assert-Equal $script:fake.StopCalls 0 'check of a foreign listener kills nothing'
Assert-LogContains 'check\.foreign' 'check of a foreign listener is logged'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$script:fake.Health = @{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'connection refused' }
$code = Invoke-Entry -Action Check
Assert-Equal $code 4 'check of an unhealthy tunnel exits 4'
Assert-Equal $script:fake.StartCalls 0 'check of an unhealthy tunnel starts nothing'
Assert-Equal $script:fake.StopCalls 0 'check of an unhealthy tunnel kills nothing'
Assert-Equal $script:fake.BrowserCalls 0 'check of an unhealthy tunnel opens no page'
Assert-LogContains 'check\.unhealthy' 'check of an unhealthy tunnel is logged'

Reset-Fake
$script:fake.ListenerThrows = $true
$code = Invoke-Entry -Action Check
Assert-Equal $code 3 'check with an unreadable listener query exits 3'
Assert-Equal $script:fake.StopCalls 0 'check with an unreadable listener query kills nothing'

Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
$code = Invoke-Entry -Action Check
Assert-Equal $code 4 'check with a live process but no listener exits 4'
Assert-StateExists 'check with a live process but no listener keeps the state'

Reset-Fake
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check with a dead state pid and no listener exits 1'
Assert-StateExists 'check with a dead state pid keeps the read-only state file'

Reset-Fake
[IO.File]::WriteAllText($script:statePath, '{bad', [Text.UTF8Encoding]::new($false))
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check with corrupt state and no listener exits 1'
Assert-StateExists 'check does not delete corrupt state'

Reset-Fake
Write-FakeState -ProcessId 4200
Add-OwnTunnel -ProcessId 4300
$code = Invoke-Entry -Action Check
Assert-Equal $code 0 'check adopts a healthy listener after a dead state pid'
Assert-StateExists 'check after a dead state pid leaves the state alone'

# ---------------------------------------------------------------------------
# -Check diagnostics: route selection and old hosts-fix necessity, read-only.
# ---------------------------------------------------------------------------

Reset-Fake
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
Assert-Equal $diagnostics.Selected 'private' 'check selects the private route when its preflight passes'
$diagnosticText = ($diagnostics.Lines -join "`n")
Assert-Match $diagnosticText 'dacintapi\.de5\.net' 'check diagnoses the deployed public domain'
Assert-Match $diagnosticText '203\.0\.113\.10' 'check prints the resolved public address'
Assert-Match $diagnosticText '(?i)hosts' 'check prints the hosts override state'
Assert-Equal $script:fake.ResolveDnsNameCalls 1 'check resolves the public domain once'
Assert-Equal $script:fake.ResolveDnsNameNames[0] 'dacintapi.de5.net' 'check resolves the deployed public domain'
Assert-Equal $script:fake.HostsOverrideCalls 1 'check reads the hosts override once'
Assert-Equal $script:fake.HostsOverrideDomains[0] 'dacintapi.de5.net' 'check reads the hosts override for the deployed domain'
Assert-Equal $script:fake.StartCalls 0 'check diagnostics start no tunnel'
Assert-Equal $script:fake.BrowserCalls 0 'check diagnostics open no page'

Reset-Fake
$script:fake.ConfigExists = $false
$script:fake.PublicConfigExists = $true
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
Assert-Equal $diagnostics.Selected 'public' 'check selects the public route when the private preflight fails'

Reset-Fake
$script:fake.ConfigExists = $false
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
Assert-Equal $diagnostics.Selected 'none' 'check reports no route when both preflights fail'

Reset-Fake
$script:fake.ResolveDnsName = @{ Ok = $false; Addresses = @(); Detail = 'DNS name does not exist' }
$script:fake.HostsOverride = @{ Exists = $true; Override = $true;
    Path = 'C:\Windows\System32\drivers\etc\hosts'; Lines = @('203.0.113.10 dacintapi.de5.net'); Detail = '' }
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
$diagnosticText = ($diagnostics.Lines -join "`n")
Assert-Match $diagnosticText 'DNS name does not exist' 'check reports a failed public DNS lookup'
Assert-Match $diagnosticText '(?i)hosts.*检测到' 'check reports an existing hosts override'
Assert-Match $diagnosticText '(?i)(不依赖|不需要|无需)' 'check explains that the entry does not require a hosts override'

Reset-Fake
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
$diagnosticText = ($diagnostics.Lines -join "`n")
Assert-Match $diagnosticText '(?i)hosts.*未发现' 'check reports that no hosts override exists'

Reset-Fake
$script:fake.ResolveDnsNameThrows = $true
$script:fake.HostsOverrideThrows = $true
$routes = New-TestRoutes
$diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $script:adapter -Routes $routes
$diagnosticText = ($diagnostics.Lines -join "`n")
Assert-Match $diagnosticText 'DNS' 'check reports a failed DNS diagnostic'
Assert-Match $diagnosticText 'hosts' 'check reports a failed hosts diagnostic'

Reset-Fake
$script:fake.ResolveDnsNameCalls = 0
$script:fake.HostsOverrideCalls = 0
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check with diagnostics still reports an absent tunnel'
Assert-Equal $script:fake.ResolveDnsNameCalls 1 'check runs the DNS diagnostic'
Assert-Equal $script:fake.HostsOverrideCalls 1 'check runs the hosts diagnostic'
Assert-Equal $script:fake.StartCalls 0 'check with diagnostics starts no tunnel'
Assert-Equal $script:fake.StopCalls 0 'check with diagnostics stops nothing'
Assert-Equal $script:fake.BrowserCalls 0 'check with diagnostics opens no page'

# ---------------------------------------------------------------------------
# -Stop lifecycle: only the positively identified own tunnel is stopped.
# ---------------------------------------------------------------------------

Reset-Fake
Add-OwnTunnel -ProcessId 4200
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop of the own tunnel exits 0'
Assert-Equal $script:fake.StopCalls 1 'stop kills exactly one process'
Assert-Equal $script:fake.StoppedPids[0] 4200 'stop kills the recorded pid'
Assert-Equal $script:fake.BrowserCalls 0 'stop opens no page'
Assert-Equal $script:fake.StartCalls 0 'stop starts nothing'
Assert-StateAbsent 'stop removes the state file'
Assert-LogContains 'stop\.stopped' 'stop logs the stopped event'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop without state but with an own listener exits 0'
Assert-Equal $script:fake.StopCalls 1 'stop without state kills the own listener'
Assert-Equal $script:fake.StoppedPids[0] 4200 'stop without state kills the listener pid'

Reset-Fake
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop without a tunnel exits 0'
Assert-Equal $script:fake.StopCalls 0 'stop without a tunnel kills nothing'
Assert-LogContains 'stop\.absent' 'stop without a tunnel logs absent'

Reset-Fake
$script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
$script:fake.Processes[999] = @{
    ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
}
$code = Invoke-Entry -Action Stop
Assert-Equal $code 3 'stop with a foreign listener exits 3'
Assert-Equal $script:fake.StopCalls 0 'stop with a foreign listener kills nothing'
Assert-LogContains 'stop\.foreign' 'stop with a foreign listener is logged'

Reset-Fake
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop with a dead state pid exits 0'
Assert-Equal $script:fake.StopCalls 0 'stop with a dead state pid kills nothing'
Assert-StateAbsent 'stop with a dead state pid removes the state'

Reset-Fake
[IO.File]::WriteAllText($script:statePath, '{bad', [Text.UTF8Encoding]::new($false))
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop with corrupt state exits 0'
Assert-Equal $script:fake.StopCalls 0 'stop with corrupt state kills nothing'
Assert-StateAbsent 'stop with corrupt state removes the state'

Reset-Fake
Write-FakeState -ProcessId 4200 -HostAlias 'other-host'
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
Add-OwnTunnel -ProcessId 4300
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop removes a stale state and stops the current tunnel'
Assert-Equal $script:fake.StopCalls 1 'stop kills only the current tunnel'
Assert-Equal $script:fake.StoppedPids[0] 4300 'stop never kills the stale pid'
Assert-StateAbsent 'stop clears the stale state'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
Write-FakeState -ProcessId 4200
$script:fake.StopSucceeds = $false
$code = Invoke-Entry -Action Stop
Assert-Equal $code 1 'stop failure exits 1'
Assert-Equal $script:fake.StopCalls 1 'stop failure was attempted once'
Assert-StateExists 'stop failure keeps the state for retry'
Assert-LogContains 'stop\.failed' 'stop failure is logged'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
Write-FakeState -ProcessId 4200
$script:fake.StopLeavesAlive = $true
$code = Invoke-Entry -Action Stop
Assert-Equal $code 1 'a tunnel that survives stop exits 1'
Assert-Equal $script:fake.StopCalls 1 'a tunnel that survives stop was signalled once'
Assert-StateExists 'a tunnel that survives stop keeps the state'

Reset-Fake
Add-OwnTunnel -ProcessId 4200
Write-FakeState -ProcessId 4200
$script:fake.StopSucceeds = $false
$script:fake.StopExitsProcess = $true
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'a process that exits during stop is treated as stopped'
Assert-Equal $script:fake.StopCalls 1 'a process that exits during stop was signalled once'
Assert-StateAbsent 'a process that exits during stop removes the state'

Reset-Fake
Write-FakeState -ProcessId 4200
$script:fake.ProcessThrows = $true
$code = Invoke-Entry -Action Stop
Assert-Equal $code 1 'stop with an unreadable state process exits 1'
Assert-Equal $script:fake.StopCalls 0 'stop with an unreadable state process kills nothing'
Assert-StateExists 'stop with an unreadable state process keeps the state'

# The state-file PID must not be killed when 18096 is owned by a different
# matching process: the listener would survive while stop reported success.
Reset-Fake
Add-OwnTunnel -ProcessId 4300
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 3 'stop with a different owned listener exits 3'
Assert-Equal $script:fake.StopCalls 0 'stop with a different owned listener kills nothing'
Assert-StateExists 'stop with a different owned listener keeps the state'
Assert-LogContains 'stop\.foreign' 'stop with a different owned listener is logged'

# A foreign listener must block stopping a live matching state PID.
Reset-Fake
$script:fake.Listeners = @(@{ LocalAddress = '127.0.0.1'; OwningProcess = 999 })
$script:fake.Processes[999] = @{
    ProcessId = 999; Name = 'other.exe'; CommandLine = 'other.exe'; Alive = $true
}
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 3 'stop with a foreign listener and live state exits 3'
Assert-Equal $script:fake.StopCalls 0 'stop with a foreign listener and live state kills nothing'
Assert-LogContains 'stop\.foreign' 'stop with a foreign listener and live state is logged'

# Unconfirmed listener ownership must block stopping a live matching state PID.
Reset-Fake
$script:fake.Listeners = @(@{ LocalAddress = 'not-an-ip'; OwningProcess = 4200 })
$script:fake.Processes[4200] = @{
    ProcessId = 4200; Name = 'ssh.exe'; Alive = $true
    CommandLine = (New-FakeTunnelCommandLine)
}
Write-FakeState -ProcessId 4200
$code = Invoke-Entry -Action Stop
Assert-Equal $code 3 'stop with unconfirmed ownership and live state exits 3'
Assert-Equal $script:fake.StopCalls 0 'stop with unconfirmed ownership and live state kills nothing'
Assert-LogContains 'stop\.foreign' 'stop with unconfirmed ownership and live state is logged'

# -Stop recognizes a public-route tunnel even though the default route is private.
Reset-Fake
Add-OwnTunnel -ProcessId 4300 -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public'
Write-FakeState -ProcessId 4300 -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public' -Route 'public'
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop of the public tunnel exits 0'
Assert-Equal $script:fake.StopCalls 1 'stop of the public tunnel kills one process'
Assert-Equal $script:fake.StoppedPids[0] 4300 'stop of the public tunnel kills the recorded pid'
Assert-StateAbsent 'stop of the public tunnel removes the state file'

Reset-Fake
Add-OwnTunnel -ProcessId 4300 -ConfigPath $script:fake.PublicConfigPath -HostAlias 'um-share-ecs-public'
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop without state recognizes a public-route listener'
Assert-Equal $script:fake.StopCalls 1 'stop without state kills the public-route listener'
Assert-Equal $script:fake.StoppedPids[0] 4300 'stop without state kills the public-route pid'

# ---------------------------------------------------------------------------
# -InstallShortcut lifecycle: a desktop shortcut to the bat entry, no secrets.
# ---------------------------------------------------------------------------

Reset-Fake
$code = Invoke-Entry -Action InstallShortcut
$expectedShortcut = Join-Path $script:fake.DesktopPath 'UM云端控制台.lnk'
Assert-Equal $code 0 'shortcut install exits 0'
Assert-Equal $script:fake.CreateCalls 1 'shortcut install creates one shortcut'
Assert-Equal $script:fake.SaveCalls 1 'shortcut install saves one shortcut'
Assert-Equal $script:fake.ShortcutPath $expectedShortcut 'shortcut uses the Chinese display name'
Assert-Equal $script:fake.Shortcut.TargetPath $script:fake.BatPath 'shortcut targets the bat entry'
Assert-Equal $script:fake.Shortcut.Arguments '' 'shortcut carries no arguments or secrets'
Assert-Equal $script:fake.Shortcut.WorkingDirectory $root 'shortcut working directory is ai-gateway'
Assert-Match $script:fake.Shortcut.Description 'UM 云端控制台' 'shortcut description is user-facing'
Assert-Match $script:fake.Shortcut.Description '私网优先，公网回退' 'shortcut description names the private-first public-fallback route'
Assert-NotMatch $script:fake.Shortcut.Description 'Tailscale' 'shortcut description is no longer Tailscale-only'
Assert-Equal $script:fake.StartCalls 0 'shortcut install starts no tunnel'
Assert-Equal $script:fake.BrowserCalls 0 'shortcut install opens no page'
Assert-Equal $script:fake.LockCalls 0 'shortcut install takes no lock'
Assert-LogContains 'shortcut\.installed' 'shortcut install is logged'

$code = Invoke-Entry -Action InstallShortcut
Assert-Equal $code 0 'shortcut install is idempotent'
Assert-Equal $script:fake.CreateCalls 2 'shortcut install recreates the same shortcut'
Assert-Equal $script:fake.SaveCalls 2 'shortcut install saves the same shortcut again'
Assert-Equal $script:fake.ShortcutPath $expectedShortcut 'shortcut path is stable'

Reset-Fake
$script:fake.BatExists = $false
$code = Invoke-Entry -Action InstallShortcut
Assert-Equal $code 1 'shortcut install without the bat entry exits 1'
Assert-Equal $script:fake.CreateCalls 0 'shortcut install without the bat creates nothing'

Reset-Fake
$code = Invoke-Entry -Action InstallShortcut -Overrides @{
    DesktopPath = (Join-Path $script:tempRoot 'no-such-desktop')
}
Assert-Equal $code 1 'shortcut install without the desktop directory exits 1'
Assert-Equal $script:fake.CreateCalls 0 'shortcut install without the desktop creates nothing'

# ---------------------------------------------------------------------------
# Preflight and lifecycle actions must not require the network or a tunnel.
# ---------------------------------------------------------------------------

Reset-Fake
$script:fake.ConfigExists = $false
$code = Invoke-Entry -Action Stop
Assert-Equal $code 0 'stop does not require the SSH config'
Assert-Equal $script:fake.EffectiveCalls 0 'stop does not run ssh -G'

Reset-Fake
$script:fake.SshMissing = $true
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check does not require ssh'
Assert-Equal $script:fake.EffectiveCalls 0 'check does not run ssh -G'

Reset-Fake
$script:fake.ConfigExists = $false
$code = Invoke-Entry -Action InstallShortcut
Assert-Equal $code 0 'shortcut install does not require the SSH config'
Assert-Equal $script:fake.EffectiveCalls 0 'shortcut install does not run ssh -G'
Assert-Equal $script:fake.TailscaleCalls 0 'shortcut install does not query Tailscale'

Reset-Fake
$script:fake.ConfigExists = $false
$code = Invoke-Entry -Action Check
Assert-Equal $code 1 'check without a config or tunnel still reports absent'
Assert-Equal $script:fake.StartCalls 0 'check without a config starts nothing'

# ---------------------------------------------------------------------------
# Console authorization: all Open paths need a fresh single-use bootstrap.
# ---------------------------------------------------------------------------

foreach ($mode in @('New', 'Adopt', 'State', 'Locked')) {
    Reset-Fake
    if ($mode -ne 'New') { Add-OwnTunnel -ProcessId 4200 }
    if ($mode -eq 'State') { Write-FakeState -ProcessId 4200 }
    if ($mode -eq 'Locked') { $script:fake.LockAvailable = $false }
    $code = Invoke-Entry
    Assert-Equal $code 0 "$mode authorization succeeds"
    Assert-Equal $script:fake.BootstrapCalls 1 "$mode signs once"
    Assert-Equal $script:fake.EffectiveCalls 1 "$mode validates SSH"
    Assert-Equal $script:fake.BootstrapSshExe $script:fake.SshExe "$mode uses resolved SSH"
    Assert-Equal $script:fake.LastUrl 'http://localhost:18096/display/grant#console=ConsoleBootstrap_0123456789' "$mode uses separate cloud browser host"
    Assert-Equal ([Uri]$script:fake.LastUrl).Host 'localhost' "$mode browser differs from local 127.0.0.1 host"
    Assert-Equal $script:fake.HealthUris[0] 'http://127.0.0.1:18096/health' "$mode health stays on IPv4 loopback"
    foreach ($flag in @('-T', '-F', 'BatchMode=yes', 'StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'UserKnownHostsFile=', 'ClearAllForwardings=yes', '--console-session')) {
        Assert-True ($script:fake.BootstrapArgumentLine.Contains($flag)) "$mode bootstrap includes $flag"
    }
    Assert-True ($script:fake.BootstrapArgumentLine.Contains($script:fake.ConfigPath)) "$mode uses dedicated config"
    Assert-True ($script:fake.BootstrapArgumentLine.Contains($script:fake.KnownHostsPath.Replace('\', '/'))) "$mode uses dedicated known hosts"
    Assert-Match $script:fake.BootstrapArgumentLine 'um-share-ecs python3 /opt/um-share/current/um-cloud-deploy/display/issue-display-grant\.py --console-session$' "$mode uses fixed remote command"
    Assert-LogNotContains 'ConsoleBootstrap_0123456789|/display/grant' "$mode bootstrap stays out of logs"
    $stateText = if (Test-Path -LiteralPath $script:statePath) { Get-Content -Raw -Encoding UTF8 $script:statePath } else { '' }
    Assert-NotMatch $stateText 'ConsoleBootstrap_0123456789|/display/grant' "$mode bootstrap stays out of state"
}

foreach ($mode in @('Adopt', 'State', 'Locked')) {
    Reset-Fake
    Add-OwnTunnel -ProcessId 4200
    if ($mode -eq 'State') { Write-FakeState -ProcessId 4200 }
    if ($mode -eq 'Locked') { $script:fake.LockAvailable = $false }
    Set-EffectiveValue 'stricthostkeychecking' 'no'
    $code = Invoke-Entry
    Assert-Equal $code 2 "$mode rejects unsafe SSH with healthy tunnel"
    Assert-Equal $script:fake.BootstrapCalls 0 "$mode unsafe SSH signs nothing"
    Assert-Equal $script:fake.BrowserCalls 0 "$mode unsafe SSH opens nothing"
}

foreach ($output in @('', 'http://evil.test/display/grant#console=ConsoleBootstrap_0123456789', '//evil.test/display/grant#console=ConsoleBootstrap_0123456789', '/display/grant#grant=ConsoleBootstrap_0123456789', '/display/grant#console=short', '/display/grant#console=ConsoleBootstrap_0123456789?bad', "/display/grant#console=ConsoleBootstrap_0123456789`nextra", ('/display/grant#console=' + ('a' * 257)))) {
    Reset-Fake
    $script:fake.BootstrapResponse.Output = $output
    $code = Invoke-Entry
    Assert-Equal $code 1 'invalid bootstrap output fails closed'
    Assert-Equal $script:fake.BrowserCalls 0 'invalid bootstrap opens no browser'
    Assert-LogNotContains 'ConsoleBootstrap_0123456789|evil\.test|/display/grant' 'invalid bootstrap remains private'
}

foreach ($failure in @('Exit', 'Throw', 'Browser')) {
    Reset-Fake
    if ($failure -eq 'Exit') { $script:fake.BootstrapResponse.ExitCode = 1 }
    if ($failure -eq 'Throw') { $script:fake.BootstrapThrows = $true }
    if ($failure -eq 'Browser') { $script:fake.BrowserThrows = $true }
    $captured = @(Invoke-Entry 6>&1)
    Assert-Equal $captured[-1] 1 "$failure authorization failure propagates"
    Assert-NotMatch ($captured -join ' ') 'ConsoleBootstrap_0123456789|/display/grant' "$failure output is sanitized"
    Assert-LogNotContains 'ConsoleBootstrap_0123456789|/display/grant' "$failure log is sanitized"
    Assert-Equal $script:fake.StopCalls 0 "$failure keeps healthy tunnel"
}

foreach ($suffix in @('', "`n", "`r`n")) {
    foreach ($length in @(16, 256)) {
        Reset-Fake
        $script:fake.BootstrapResponse.Output = '/display/grant#console=' + ('a' * $length) + $suffix
        $code = Invoke-Entry
        Assert-Equal $code 0 "bootstrap length $length permits only helper print newline"
        Assert-Equal $script:fake.LastUrl ('http://localhost:18096/display/grant#console=' + ('a' * $length)) 'browser URL excludes helper print newline'
    }
}

foreach ($action in @('Check', 'Stop', 'InstallShortcut')) {
    Reset-Fake
    Add-OwnTunnel -ProcessId 4200
    $null = Invoke-Entry -Action $action
    Assert-Equal $script:fake.BootstrapCalls 0 "$action never signs authorization"
}

# Exercise the real ProcessStartInfo boundary with a harmless local child.
# The remote SSH boundary stays fake; no credentials or network are accessed.
$childExe = (Get-Process -Id $PID).Path
function New-ChildArguments {
    param([string]$Command)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    return '-NoProfile -NonInteractive -EncodedCommand ' + $encoded
}
$arguments = New-ChildArguments "[Console]::Out.WriteLine('/display/grant#console=ConsoleBootstrap_0123456789'); [Console]::Error.WriteLine('PRIVATE-SSH-STDERR'); exit 0"
$response = & $script:CloudConsoleDefaultAdapter.IssueConsoleBootstrap $childExe $arguments
Assert-Equal $response.ExitCode 0 'real process adapter propagates successful exit'
Assert-Match $response.Output '\A/display/grant#console=ConsoleBootstrap_0123456789\r?\n\z' 'real process adapter captures stdout in memory'
Assert-NotMatch ($response | ConvertTo-Json) 'PRIVATE-SSH-STDERR' 'real process adapter never returns stderr'
$arguments = New-ChildArguments "[Console]::Error.WriteLine('PRIVATE-SSH-STDERR'); exit 7"
$response = Invoke-CloudConsoleBootstrapProcess -SshExe $childExe -ArgumentLine $arguments
Assert-Equal $response.ExitCode 7 'real process adapter propagates nonzero exit'
Assert-NotMatch ($response | ConvertTo-Json) 'PRIVATE-SSH-STDERR' 'failed real process adapter keeps stderr private'
$arguments = New-ChildArguments 'Start-Sleep -Seconds 30'
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$caught = ''
try { $null = Invoke-CloudConsoleBootstrapProcess -SshExe $childExe -ArgumentLine $arguments -TimeoutMilliseconds 100 }
catch { $caught = $_.Exception.Message }
$stopwatch.Stop()
Assert-Equal $caught 'Console authorization could not be obtained' 'bootstrap timeout has fixed failure text'
Assert-True ($stopwatch.Elapsed.TotalSeconds -lt 10) 'bootstrap timeout terminates bounded local child'

# A successful parent exit does not complete redirected pipes inherited by a
# descendant. The process wait and both stream reads share one deadline.
$heldPipePidPath = Join-Path $script:tempRoot 'held-pipe-child.pid'
$heldPipeParentPidPath = Join-Path $script:tempRoot 'held-pipe-parent.pid'
$descendantCommand = "[IO.File]::WriteAllText('$($heldPipePidPath.Replace("'", "''"))', [string]`$PID); Start-Sleep -Seconds 5"
$descendantArguments = New-ChildArguments $descendantCommand
$parentCommand = @'
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = '{0}'
$startInfo.Arguments = '{1}'
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$child = [Diagnostics.Process]::Start($startInfo)
$child.Dispose()
[IO.File]::WriteAllText('{2}', [string]$PID)
exit 0
'@ -f $childExe.Replace("'", "''"), $descendantArguments, $heldPipeParentPidPath.Replace("'", "''")
$arguments = New-ChildArguments $parentCommand
$fixtureStarted = [DateTime]::UtcNow
$stopwatch.Restart()
$caught = ''
try {
    try { $null = Invoke-CloudConsoleBootstrapProcess -SshExe $childExe -ArgumentLine $arguments -TimeoutMilliseconds 1000 }
    catch { $caught = $_.Exception.Message }
    $stopwatch.Stop()
    Assert-True (Test-Path -LiteralPath $heldPipePidPath) 'held-pipe descendant actually started'
    Assert-True (Test-Path -LiteralPath $heldPipeParentPidPath) 'held-pipe parent reached its successful exit'
    $fixtureParentPid = [int]([IO.File]::ReadAllText($heldPipeParentPidPath))
    Assert-True ($null -eq (Get-Process -Id $fixtureParentPid -ErrorAction SilentlyContinue)) 'held-pipe parent has exited before adapter returns'
    Assert-Equal $caught 'Console authorization could not be obtained' 'inherited redirected pipes time out with fixed failure'
    Assert-True ($stopwatch.Elapsed.TotalSeconds -lt 3) 'parent exit cannot bypass redirected-pipe deadline'
} finally {
    # Only terminate the child identified by this fixture, and reject a reused
    # PID unless executable and creation time still match this test invocation.
    if (Test-Path -LiteralPath $heldPipePidPath) {
        $fixturePid = [int]([IO.File]::ReadAllText($heldPipePidPath))
        $fixtureProcess = Get-Process -Id $fixturePid -ErrorAction SilentlyContinue
        if ($null -ne $fixtureProcess -and $fixtureProcess.Path -eq $childExe -and
                $fixtureProcess.StartTime.ToUniversalTime() -ge $fixtureStarted) {
            $fixtureProcess.Kill()
            $null = $fixtureProcess.WaitForExit(2000)
        }
    }
}

# ---------------------------------------------------------------------------
# Summary.
# ---------------------------------------------------------------------------

if (Test-Path -LiteralPath $script:tempRoot) {
    Remove-Item -LiteralPath $script:tempRoot -Recurse -Force
}

if ($script:failures -gt 0) {
    Write-Host ("cloud console entry: {0} of {1} check(s) failed" -f $script:failures, $script:checks) -ForegroundColor Red
    exit 1
}
Write-Host ("cloud console entry: all {0} checks passed" -f $script:checks) -ForegroundColor Green
exit 0
