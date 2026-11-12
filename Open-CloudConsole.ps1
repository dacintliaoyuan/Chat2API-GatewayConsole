# Desktop entry for the UM cloud console.
#
# Opens a loopback SSH tunnel (127.0.0.1:18096 -> cloud 127.0.0.1:18096),
# waits for the cloud console health endpoint, then authorizes Edge with a
# short-lived single-use bootstrap. The tunnel prefers the private Tailscale SSH
# config and falls back to the public SSH config only when the private route cannot
# be preflighted or started. Both routes keep the same strict host verification
# and a dedicated key/known_hosts file. The browser exchanges the bootstrap for
# the existing HttpOnly console session before redirecting to
# http://localhost:18096/#overview. The distinct browser host preserves the
# default local console's 127.0.0.1 session cookie.
#
# Security boundaries:
#   * Each dedicated SSH config is passed explicitly with -F; the default
#     ~/.ssh/config is never used.
#   * BatchMode=yes, StrictHostKeyChecking=yes, IdentitiesOnly=yes and the
#     dedicated known_hosts file are enforced on the command line and checked
#     through `ssh -G` for both routes. Only the private route additionally
#     requires a Tailscale 100.64.0.0/10 HostName.
#   * Only a listener on 127.0.0.1:18096 owned by an ssh.exe process whose
#     command line matches the selected route's config/host/forward is reused or
#     stopped. Foreign and unconfirmed owners are reported, never killed.
#   * No long-lived token or key material is read. The short-lived bootstrap
#     stays in memory and the browser fragment; state and logs never contain it.
#   * Tailscale and the local proxy are never started or restarted. The entry
#     only reads `tailscale status --json` when the private route starts a tunnel.
#   * The old control-domain hosts fix is diagnosed read-only by -Check; the
#     entry never writes hosts or hardcodes the retired control IP/domain.
#
# Exit codes:
#   0 success / healthy / stopped / shortcut installed
#   1 runtime failure (ssh exited, health timeout, browser or state failure)
#   2 preflight failure (ssh, config, key, known_hosts, Tailscale)
#   3 port 18096 is foreign or its owner cannot be confirmed
#   4 our tunnel process is alive but the health check failed
#
# -Library is a test-only switch: it loads the functions without running the
# entry. The offline regression test dot-sources this file with -Library.

[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Check,
    [switch]$InstallShortcut,
    [switch]$Library,
    [string]$SshConfig = (Join-Path $HOME '.ssh\aliyun-um-ecs.conf'),
    [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$SshHostAlias = 'um-share-ecs',
    [string]$PublicSshConfig = (Join-Path $HOME '.ssh\aliyun-um-ecs-public.conf'),
    [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$PublicSshHostAlias = 'um-share-ecs-public',
    [string]$StatePath = (Join-Path $PSScriptRoot 'data\cloud-console-tunnel.json'),
    [string]$LogPath = (Join-Path $PSScriptRoot 'data\cloud-console-entry.log'),
    [string]$DesktopPath = '',
    [string]$ShortcutName = 'UM云端控制台',
    [ValidateRange(1, 300)][int]$HealthTimeoutSeconds = 25,
    [ValidateRange(50, 5000)][int]$HealthPollMilliseconds = 500,
    [ValidateRange(1, 60)][int]$StopTimeoutSeconds = 10,
    [ValidateRange(100, 120000)][int]$LockTimeoutMilliseconds = 15000,
    [hashtable]$Adapter
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$script:CloudConsoleRoot = $PSScriptRoot
$script:CloudConsoleBatPath = Join-Path $PSScriptRoot 'Open-CloudConsole.bat'
$script:CloudConsoleBind = '127.0.0.1'
$script:CloudConsolePort = 18096
$script:CloudConsoleTargetPort = 18096
$script:CloudConsoleHealthUrl = 'http://127.0.0.1:18096/health'
$script:CloudConsoleConfigPath = (Join-Path $HOME '.ssh\aliyun-um-ecs.conf')
$script:CloudConsoleHostAlias = 'um-share-ecs'
$script:CloudConsolePublicConfigPath = (Join-Path $HOME '.ssh\aliyun-um-ecs-public.conf')
$script:CloudConsolePublicHostAlias = 'um-share-ecs-public'
$script:CloudConsolePublicDomain = 'dacintapi.de5.net'
$script:CloudConsoleShortcutName = 'UM云端控制台'
$script:CloudConsoleStatePath = (Join-Path $PSScriptRoot 'data\cloud-console-tunnel.json')
$script:CloudConsoleLogPath = (Join-Path $PSScriptRoot 'data\cloud-console-entry.log')
$script:CloudConsoleLockName = 'Local\UMCloudConsoleEntry'

function Get-CloudObjectProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        foreach ($key in $Object.Keys) {
            if ([string]$key -ieq $Name) { return $Object[$key] }
        }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function New-CloudResult {
    param([int]$ExitCode, [string]$Label, [string]$Event, [string]$Message)
    return @{
        ExitCode = $ExitCode
        Label = $Label
        Event = $Event
        Message = $Message
    }
}

function Write-CloudConsoleLog {
    param([string]$Level, [string]$Event, [string]$Message, [string]$LogPath)
    if ([string]::IsNullOrWhiteSpace($LogPath)) { return }
    $line = '{0} [{1}] {2} {3}' -f [DateTime]::UtcNow.ToString('o'), $Level, $Event, $Message
    try {
        $directory = Split-Path -Parent $LogPath
        if ($directory -and -not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        [IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "无法写入入口日志 ${LogPath}: $($_.Exception.Message)"
    }
}

function Get-CloudEffectiveValues {
    param($Effective, [string]$Key)
    if ($null -eq $Effective) { return @() }
    $raw = $null
    if ($Effective -is [System.Collections.IDictionary]) {
        foreach ($candidate in $Effective.Keys) {
            if ([string]$candidate -ieq $Key) { $raw = $Effective[$candidate]; break }
        }
    } else {
        $property = $Effective.PSObject.Properties[$Key]
        if ($null -ne $property) { $raw = $property.Value }
    }
    $values = @()
    foreach ($value in @($raw)) {
        $text = [string]$value
        if (-not [string]::IsNullOrWhiteSpace($text)) { $values += $text }
    }
    return $values
}

function Get-CloudEffectiveValue {
    param($Effective, [string]$Key)
    $values = @(Get-CloudEffectiveValues -Effective $Effective -Key $Key)
    if ($values.Count -eq 0) { return '' }
    return [string]$values[0]
}

function Test-CloudTailscaleAddress {
    param([string]$Address)
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$ip)) { return $false }
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { return $false }
    $bytes = $ip.GetAddressBytes()
    return ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127)
}

function Test-CloudPublicHost {
    param([string]$HostName)
    if ([string]::IsNullOrWhiteSpace($HostName)) { return $false }
    $ip = $null
    if ([Net.IPAddress]::TryParse($HostName, [ref]$ip)) { return $true }
    $trimmed = $HostName.TrimEnd('.')
    if ($trimmed.Length -eq 0 -or $trimmed.Length -gt 253) { return $false }
    return ($trimmed -match '\A(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\z')
}

function New-CloudConsoleRoute {
    param([string]$Name, [string]$ConfigPath, [string]$HostAlias, [bool]$RequireTailscaleAddress)
    return @{
        Name = $Name
        ConfigPath = $ConfigPath
        HostAlias = $HostAlias
        RequireTailscaleAddress = $RequireTailscaleAddress
    }
}

function Get-CloudConsoleRoutes {
    param([string]$SshConfig, [string]$SshHostAlias, [string]$PublicSshConfig, [string]$PublicSshHostAlias)
    return @(
        (New-CloudConsoleRoute -Name 'private' -ConfigPath $SshConfig -HostAlias $SshHostAlias `
            -RequireTailscaleAddress $true),
        (New-CloudConsoleRoute -Name 'public' -ConfigPath $PublicSshConfig -HostAlias $PublicSshHostAlias `
            -RequireTailscaleAddress $false)
    )
}

function Get-CloudConsoleRoute {
    param($Routes, [string]$Name)
    foreach ($route in @($Routes)) {
        if ([string](Get-CloudObjectProperty -Object $route -Name 'Name') -eq $Name) { return $route }
    }
    return $null
}

function Test-CloudSamePath {
    param([string]$Left, [string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    $leftNormalized = $Left.Replace('/', '\').TrimEnd('\')
    $rightNormalized = $Right.Replace('/', '\').TrimEnd('\')
    return [string]::Equals($leftNormalized, $rightNormalized, [StringComparison]::OrdinalIgnoreCase)
}

function Get-CloudConsoleStateRoute {
    param($State, $Routes)
    if ($null -eq $State) { return '' }
    $recorded = [string](Get-CloudObjectProperty -Object $State -Name 'route')
    if (-not [string]::IsNullOrWhiteSpace($recorded)) {
        if ($recorded -ne 'private' -and $recorded -ne 'public') { return '' }
        $stateConfig = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_config')
        $stateHost = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_host')
        if ([string]::IsNullOrWhiteSpace($stateConfig) -and [string]::IsNullOrWhiteSpace($stateHost)) {
            return $recorded
        }
        $routeDefinition = Get-CloudConsoleRoute -Routes $Routes -Name $recorded
        if ($null -ne $routeDefinition -and
                (Test-CloudSamePath -Left $stateConfig -Right ([string]$routeDefinition.ConfigPath)) -and
                [string]::Equals($stateHost, [string]$routeDefinition.HostAlias, [StringComparison]::OrdinalIgnoreCase)) {
            return $recorded
        }
        return ''
    }
    $stateConfig = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_config')
    $stateHost = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_host')
    if ([string]::IsNullOrWhiteSpace($stateConfig) -or [string]::IsNullOrWhiteSpace($stateHost)) {
        return ''
    }
    foreach ($route in @($Routes)) {
        if ((Test-CloudSamePath -Left $stateConfig -Right ([string]$route.ConfigPath)) -and
                [string]::Equals($stateHost, [string]$route.HostAlias, [StringComparison]::OrdinalIgnoreCase)) {
            return [string]$route.Name
        }
    }
    return ''
}

function Get-CloudSshExecutable {
    $command = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw '未找到 ssh.exe（OpenSSH 客户端）' }
    $path = [string]$command.Source
    if ([string]::IsNullOrWhiteSpace($path)) { $path = [string]$command.Path }
    if ([string]::IsNullOrWhiteSpace($path)) { throw 'ssh.exe 路径不可用' }
    return $path
}

function Get-CloudSshEffectiveConfig {
    param([string]$SshExe, [string]$ConfigPath, [string]$HostAlias)
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'SSH 配置路径为空' }
    $previous = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $SshExe -G -F $ConfigPath $HostAlias 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) { throw "ssh -G 失败（exit $exitCode）" }
    $map = @{}
    foreach ($line in $output) {
        $text = [string]$line
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        $match = [regex]::Match($text, '^(\S+)\s+(.+)$')
        if (-not $match.Success) { continue }
        $key = $match.Groups[1].Value.ToLowerInvariant()
        $value = $match.Groups[2].Value.Trim()
        if (-not $map.ContainsKey($key)) { $map[$key] = @() }
        $map[$key] = @($map[$key]) + $value
    }
    if ($map.Count -eq 0) { throw 'ssh -G 没有返回可解析的配置' }
    return $map
}

function Test-CloudPathExists {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    return (Test-Path -LiteralPath $Path -PathType Leaf)
}

function Assert-CloudSshEffectiveConfig {
    param($Adapter, $Effective, [string]$ConfigPath, [bool]$RequireTailscaleAddress = $true)
    $problems = New-Object System.Collections.Generic.List[string]
    if ((Get-CloudEffectiveValue -Effective $Effective -Key 'batchmode') -ne 'yes') {
        $problems.Add('BatchMode 必须为 yes')
    }
    $strict = Get-CloudEffectiveValue -Effective $Effective -Key 'stricthostkeychecking'
    if ($strict -notin @('yes', 'true')) {
        $problems.Add('StrictHostKeyChecking 必须为 yes')
    }
    if ((Get-CloudEffectiveValue -Effective $Effective -Key 'identitiesonly') -ne 'yes') {
        $problems.Add('IdentitiesOnly 必须为 yes')
    }
    if ((Get-CloudEffectiveValue -Effective $Effective -Key 'tcpkeepalive') -ne 'yes') {
        $problems.Add('TCPKeepAlive 必须为 yes')
    }
    $identityFiles = @(Get-CloudEffectiveValues -Effective $Effective -Key 'identityfile')
    if ($identityFiles.Count -eq 0) {
        $problems.Add('缺少 IdentityFile（原密钥）')
    } else {
        $identityFile = [string]$identityFiles[0]
        $exists = $false
        try { $exists = [bool](& $Adapter.TestPath $identityFile) }
        catch { $problems.Add("无法检查原密钥文件: $($_.Exception.Message)") }
        if (-not $exists) { $problems.Add("原密钥文件不存在: $identityFile") }
    }
    $knownHosts = @(Get-CloudEffectiveValues -Effective $Effective -Key 'userknownhostsfile')
    if ($knownHosts.Count -eq 0) {
        $problems.Add('缺少 UserKnownHostsFile（专用 known_hosts）')
    } else {
        $knownHostsFile = [string]$knownHosts[0]
        $exists = $false
        try { $exists = [bool](& $Adapter.TestPath $knownHostsFile) }
        catch { $problems.Add("无法检查 known_hosts 文件: $($_.Exception.Message)") }
        if (-not $exists) { $problems.Add("known_hosts 文件不存在: $knownHostsFile") }
    }
    $hostName = Get-CloudEffectiveValue -Effective $Effective -Key 'hostname'
    if ([string]::IsNullOrWhiteSpace($hostName)) {
        $problems.Add('缺少 HostName')
    } elseif ($RequireTailscaleAddress) {
        if (-not (Test-CloudTailscaleAddress -Address $hostName)) {
            $problems.Add("HostName 不在 Tailscale 100.64.0.0/10: $hostName")
        }
    } elseif (-not (Test-CloudPublicHost -HostName $hostName)) {
        $problems.Add("HostName 不是合法 DNS 名或 IP: $hostName")
    }
    if ([string]::IsNullOrWhiteSpace((Get-CloudEffectiveValue -Effective $Effective -Key 'user'))) {
        $problems.Add('缺少 User')
    }
    $port = 0
    if (-not [int]::TryParse((Get-CloudEffectiveValue -Effective $Effective -Key 'port'), [ref]$port) -or
            $port -lt 1 -or $port -gt 65535) {
        $problems.Add('Port 必须是 1-65535')
    }
    $connectTimeout = 0
    if (-not [int]::TryParse((Get-CloudEffectiveValue -Effective $Effective -Key 'connecttimeout'), [ref]$connectTimeout) -or
            $connectTimeout -lt 1) {
        $problems.Add('ConnectTimeout 必须为正整数')
    }
    $serverAliveInterval = 0
    if (-not [int]::TryParse((Get-CloudEffectiveValue -Effective $Effective -Key 'serveraliveinterval'), [ref]$serverAliveInterval) -or
            $serverAliveInterval -lt 1) {
        $problems.Add('ServerAliveInterval 必须为正整数')
    }
    $serverAliveCountMax = 0
    if (-not [int]::TryParse((Get-CloudEffectiveValue -Effective $Effective -Key 'serveralivecountmax'), [ref]$serverAliveCountMax) -or
            $serverAliveCountMax -lt 1) {
        $problems.Add('ServerAliveCountMax 必须为正整数')
    }
    return @{ Ok = ($problems.Count -eq 0); Problems = @($problems) }
}

function Get-CloudTailscaleStatus {
    param([string]$TargetAddress)
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return @{ Available = $false; Detail = 'tailscale CLI 不在 PATH' }
    }
    $source = [string]$command.Source
    if ([string]::IsNullOrWhiteSpace($source)) { $source = [string]$command.Path }
    if ([string]::IsNullOrWhiteSpace($source)) {
        return @{ Available = $false; Detail = 'tailscale CLI 路径不可用' }
    }
    $previous = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $source status --json 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        return @{ Available = $false; Detail = "tailscale status 退出码 $exitCode" }
    }
    $payload = $null
    try { $payload = (($output -join [Environment]::NewLine) | ConvertFrom-Json) }
    catch { return @{ Available = $false; Detail = 'tailscale status 输出无法解析' } }
    $peerFound = $false
    $peerOnline = $false
    $peerContainer = Get-CloudObjectProperty -Object $payload -Name 'Peer'
    if ($null -ne $peerContainer) {
        if ($peerContainer -is [System.Collections.IDictionary]) {
            $peers = @($peerContainer.Values)
        } else {
            $peers = @($peerContainer.PSObject.Properties | ForEach-Object { $_.Value })
        }
        foreach ($peer in $peers) {
            $ips = @(Get-CloudObjectProperty -Object $peer -Name 'TailscaleIPs')
            $hostName = [string](Get-CloudObjectProperty -Object $peer -Name 'HostName')
            if (($ips -contains $TargetAddress) -or ($hostName -eq $TargetAddress)) {
                $peerFound = $true
                $peerOnline = [bool](Get-CloudObjectProperty -Object $peer -Name 'Online')
                break
            }
        }
    }
    $self = Get-CloudObjectProperty -Object $payload -Name 'Self'
    return @{
        Available = $true
        BackendState = [string](Get-CloudObjectProperty -Object $payload -Name 'BackendState')
        SelfOnline = [bool](Get-CloudObjectProperty -Object $self -Name 'Online')
        PeerFound = $peerFound
        PeerOnline = $peerOnline
        Detail = ''
    }
}

function Get-CloudTailscalePreflight {
    param($Adapter, [string]$TargetAddress)
    $status = $null
    try { $status = & $Adapter.GetTailscaleStatus $TargetAddress }
    catch {
        return @{ Ok = $true; Detail = "无法预检 Tailscale（$($_.Exception.Message)）；继续尝试 SSH 隧道。" }
    }
    if ($null -eq $status -or -not [bool](Get-CloudObjectProperty -Object $status -Name 'Available')) {
        $detail = [string](Get-CloudObjectProperty -Object $status -Name 'Detail')
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = '未找到 tailscale CLI' }
        return @{ Ok = $true; Detail = "$detail；继续尝试 SSH 隧道。" }
    }
    $backendState = [string](Get-CloudObjectProperty -Object $status -Name 'BackendState')
    if ($backendState -ne 'Running') {
        return @{ Ok = $false; Detail = "Tailscale 状态为 '$backendState'；请手动启动或登录 Tailscale，本入口不会自动启动。" }
    }
    if (-not [bool](Get-CloudObjectProperty -Object $status -Name 'SelfOnline')) {
        return @{ Ok = $false; Detail = '本机 Tailscale 未在线；请检查 Tailscale 及其控制连接可用的本地代理，本入口不会自动启动。' }
    }
    if ([bool](Get-CloudObjectProperty -Object $status -Name 'PeerFound') -and
            -not [bool](Get-CloudObjectProperty -Object $status -Name 'PeerOnline')) {
        return @{ Ok = $false; Detail = "目标 $TargetAddress 在 Tailscale 中离线；请确认云机与 Tailscale 在线。" }
    }
    return @{ Ok = $true; Detail = 'Tailscale 预检通过。' }
}

function Resolve-CloudDnsName {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) {
        return @{ Ok = $false; Addresses = @(); Detail = '域名为空' }
    }
    $records = @()
    try {
        $records = @(Resolve-DnsName -Name $Name -Type A -ErrorAction Stop)
    } catch {
        return @{ Ok = $false; Addresses = @(); Detail = $_.Exception.Message }
    }
    $addresses = @($records | ForEach-Object { [string]$_.IPAddress } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    return @{ Ok = ($addresses.Count -gt 0); Addresses = $addresses; Detail = '' }
}

function Test-CloudHostsOverride {
    param([string]$Domain)
    if ([string]::IsNullOrWhiteSpace($Domain)) {
        return @{ Exists = $false; Override = $false; Path = ''; Lines = @(); Detail = '域名为空' }
    }
    $windows = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ([string]::IsNullOrWhiteSpace($windows)) {
        return @{ Exists = $false; Override = $false; Path = ''; Lines = @(); Detail = '无法定位 Windows 目录' }
    }
    $hostsPath = Join-Path $windows 'System32\drivers\etc\hosts'
    if (-not (Test-Path -LiteralPath $hostsPath -PathType Leaf)) {
        return @{ Exists = $false; Override = $false; Path = $hostsPath; Lines = @(); Detail = 'hosts 文件不存在' }
    }
    $lines = @()
    try { $lines = @(Get-Content -LiteralPath $hostsPath -ErrorAction Stop) }
    catch {
        return @{ Exists = $true; Override = $false; Path = $hostsPath; Lines = @();
            Detail = "hosts 读取失败：$($_.Exception.Message)" }
    }
    $pattern = '(?i)^\s*[0-9a-f:.]+\s+' + [regex]::Escape($Domain) + '(?:\s|$)'
    $overrideLines = @($lines | Where-Object { $_ -match $pattern })
    return @{ Exists = $true; Override = ($overrideLines.Count -gt 0); Path = $hostsPath;
        Lines = $overrideLines; Detail = '' }
}

function Get-CloudListenerSnapshot {
    param([int]$Port)
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        if ([string]$_.CategoryInfo.Category -eq 'ObjectNotFound' -and
                [string]$_.FullyQualifiedErrorId -match 'CmdletizationQuery_NotFound') {
            return @()
        }
        throw
    }
}

function Get-CloudProcessInfo {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $cim = $null
    try { $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop }
    catch { $cim = $null }
    $name = if ($null -ne $cim) { [string]$cim.Name } else { [string]$process.ProcessName }
    $commandLine = if ($null -ne $cim) { [string]$cim.CommandLine } else { '' }
    return @{ ProcessId = [int]$ProcessId; Name = $name; CommandLine = $commandLine; Alive = $true }
}

function Start-CloudTunnelProcess {
    param([string]$SshExe, [string]$ArgumentLine, [string]$StdoutPath, [string]$StderrPath)
    $directory = Split-Path -Parent $StdoutPath
    if ($directory -and -not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $process = Start-Process -FilePath $SshExe -ArgumentList $ArgumentLine `
        -WindowStyle Hidden -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath `
        -PassThru -ErrorAction Stop
    if ($null -eq $process) { throw 'Start-Process 未返回进程' }
    return @{ ProcessId = [int]$process.Id }
}

function Stop-CloudTunnelProcess {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $false }
    try {
        Stop-Process -Id $ProcessId -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Invoke-CloudHealthRequest {
    param([string]$Uri, [int]$TimeoutSeconds)
    try {
        $parameters = @{ Uri = $Uri; TimeoutSec = $TimeoutSeconds; UseBasicParsing = $true }
        $command = Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue
        if ($null -ne $command -and $command.Parameters.ContainsKey('NoProxy')) {
            $parameters['NoProxy'] = $true
        }
        $response = Invoke-WebRequest @parameters
        return @{
            Ok = $true
            StatusCode = [int]$response.StatusCode
            Content = [string]$response.Content
            Detail = ''
        }
    } catch {
        return @{ Ok = $false; StatusCode = 0; Content = ''; Detail = $_.Exception.Message }
    }
}

function Open-CloudBrowser {
    param([string]$Url)
    $edge = ''
    $command = Get-Command msedge.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $edge = [string]$command.Source
        if ([string]::IsNullOrWhiteSpace($edge)) { $edge = [string]$command.Path }
    }
    if ([string]::IsNullOrWhiteSpace($edge)) {
        $candidates = @()
        $programFiles = [Environment]::GetEnvironmentVariable('ProgramFiles')
        $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
        if ($programFiles) { $candidates += (Join-Path $programFiles 'Microsoft\Edge\Application\msedge.exe') }
        if ($programFilesX86) { $candidates += (Join-Path $programFilesX86 'Microsoft\Edge\Application\msedge.exe') }
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $edge = $candidate; break }
        }
    }
    if ([string]::IsNullOrWhiteSpace($edge)) {
        Write-Warning '未找到 Edge，改用系统默认浏览器打开。'
        Start-Process -FilePath $Url -ErrorAction Stop
        return $true
    }
    Start-Process -FilePath $edge -ArgumentList @('--new-window', $Url) -ErrorAction Stop
    return $true
}

function Invoke-CloudConsoleBootstrapProcess {
    param([string]$SshExe, [string]$ArgumentLine,
          [ValidateRange(1, 120000)][int]$TimeoutMilliseconds = 20000)
    # This short SSH command must never use the tunnel's redirected log files.
    $process = [Diagnostics.Process]::new()
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    try {
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $SshExe
        $startInfo.Arguments = $ArgumentLine
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw 'Bootstrap process did not start' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $remaining = [int][Math]::Max(0, $TimeoutMilliseconds - $deadline.ElapsedMilliseconds)
        if ($remaining -eq 0 -or -not $process.WaitForExit($remaining)) { throw 'Bootstrap process timed out' }
        # Descendants such as ProxyCommand may retain the pipes after ssh exits.
        # Waiting for EOF must consume the same deadline, not an unbounded read.
        $streams = [Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]@($stdout, $stderr))
        $remaining = [int][Math]::Max(0, $TimeoutMilliseconds - $deadline.ElapsedMilliseconds)
        if ($remaining -eq 0 -or -not $streams.Wait($remaining)) { throw 'Bootstrap streams timed out' }
        # Both reads are complete here; these calls only retrieve their results.
        $output = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        return @{ ExitCode = $process.ExitCode; Output = $output }
    } catch {
        # Process and SSH exceptions can include the URL or remote stderr.
        throw 'Console authorization could not be obtained'
    } finally {
        # Close our pipe handles even if an exited parent's descendants retain
        # theirs. Never wait for stream tasks during failure cleanup.
        try { $process.StandardOutput.Dispose() } catch { }
        try { $process.StandardError.Dispose() } catch { }
        try {
            if ($null -ne $process -and -not $process.HasExited) {
                $process.Kill()
                $null = $process.WaitForExit(2000)
            }
        } catch { }
        $process.Dispose()
    }
}

function Get-CloudConsoleBootstrapArgumentLine {
    param([string]$ConfigPath, [string]$HostAlias, [string]$KnownHostsPath)
    # Use Windows process-argument quoting, including trailing backslashes.
    $quoted = @()
    $knownHostsOption = 'UserKnownHostsFile="' + $KnownHostsPath.Replace('\', '/').Replace('"', '\"') + '"'
    foreach ($value in @($ConfigPath, $knownHostsOption)) {
        $escaped = [regex]::Replace($value, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        $quoted += '"' + $escaped + '"'
    }
    return ('-T -F {0} -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes ' +
        '-o {1} -o ClearAllForwardings=yes -o ConnectTimeout=15 ' +
        '{2} python3 /opt/um-share/current/um-cloud-deploy/display/issue-display-grant.py --console-session') -f `
        $quoted[0], $quoted[1], $HostAlias
}

function Open-CloudConsoleAuthenticatedPage {
    param($Adapter, $SshPreflight, [string]$ConfigPath, [string]$HostAlias)
    $failure = New-CloudResult 1 '授权' 'open.authorizationfailed' '云端控制台授权失败；请检查专用 SSH 连接与云端授权服务后重试。'
    try {
        $knownHosts = Get-CloudEffectiveValue -Effective $SshPreflight.Effective -Key 'userknownhostsfile'
        $arguments = Get-CloudConsoleBootstrapArgumentLine -ConfigPath $ConfigPath -HostAlias $HostAlias -KnownHostsPath $knownHosts
        $response = & $Adapter.IssueConsoleBootstrap $SshPreflight.SshExe $arguments
        if ($null -eq $response -or (Get-CloudObjectProperty $response 'ExitCode') -ne 0) { return $failure }
        $relativeUrl = [string](Get-CloudObjectProperty $response 'Output')
        # Permit the helper's single print newline, never multiple lines or a
        # second destination. Avoid Trim() accepting arbitrary surrounding input.
        if ($relativeUrl -cnotmatch '\A/display/grant#console=[A-Za-z0-9_-]{16,256}(?:\r?\n)?\z') { return $failure }
        $url = 'http://localhost:18096' + $relativeUrl.TrimEnd("`r", "`n")
        $browser = Open-CloudConsolePage -Adapter $Adapter -Url $url
        if (-not $browser.Ok) {
            return New-CloudResult 1 '错误' 'open.browserfailed' '隧道已就绪，但打开 Edge 失败；请重新运行云端控制台入口。'
        }
        return New-CloudResult 0 '入口' 'open.ready' 'SSH 隧道已就绪，已打开 UM 云端控制台授权页面。'
    } catch {
        return $failure
    }
}

function Enter-CloudEntryLock {
    param([string]$Name, [int]$TimeoutMilliseconds)
    $mutex = $null
    try {
        $mutex = New-Object System.Threading.Mutex($false, $Name)
        if ($mutex.WaitOne($TimeoutMilliseconds)) { return $mutex }
        $mutex.Dispose()
        return $null
    } catch [System.Threading.AbandonedMutexException] {
        return $mutex
    } catch {
        if ($null -ne $mutex) { $mutex.Dispose() }
        throw
    }
}

function Exit-CloudEntryLock {
    param($Lock)
    if ($null -eq $Lock) { return }
    try { $Lock.ReleaseMutex() }
    catch { Write-Warning "释放入口互斥锁失败: $($_.Exception.Message)" }
    finally { $Lock.Dispose() }
}

function Get-CloudDesktopPath {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ([string]::IsNullOrWhiteSpace($desktop)) { throw '无法定位桌面目录' }
    return $desktop
}

function New-CloudShortcutShell {
    return (New-Object -ComObject WScript.Shell)
}

$script:CloudConsoleDefaultAdapter = @{
    ResolveSsh = { Get-CloudSshExecutable }
    GetEffectiveConfig = {
        param($SshExe, $ConfigPath, $HostAlias)
        Get-CloudSshEffectiveConfig -SshExe $SshExe -ConfigPath $ConfigPath -HostAlias $HostAlias
    }
    TestPath = { param($Path) Test-CloudPathExists -Path $Path }
    GetListeners = { param($Port) Get-CloudListenerSnapshot -Port $Port }
    GetProcess = { param($ProcessId) Get-CloudProcessInfo -ProcessId $ProcessId }
    StartTunnel = {
        param($SshExe, $ArgumentLine, $StdoutPath, $StderrPath)
        Start-CloudTunnelProcess -SshExe $SshExe -ArgumentLine $ArgumentLine -StdoutPath $StdoutPath -StderrPath $StderrPath
    }
    StopProcess = { param($ProcessId) Stop-CloudTunnelProcess -ProcessId $ProcessId }
    TestHealth = { param($Uri, $TimeoutSeconds) Invoke-CloudHealthRequest -Uri $Uri -TimeoutSeconds $TimeoutSeconds }
    OpenBrowser = { param($Url) Open-CloudBrowser -Url $Url }
    IssueConsoleBootstrap = {
        param($SshExe, $ArgumentLine)
        Invoke-CloudConsoleBootstrapProcess -SshExe $SshExe -ArgumentLine $ArgumentLine
    }
    Sleep = { param($Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
    EnterLock = {
        param($Name, $TimeoutMilliseconds)
        Enter-CloudEntryLock -Name $Name -TimeoutMilliseconds $TimeoutMilliseconds
    }
    ExitLock = { param($Lock) Exit-CloudEntryLock -Lock $Lock }
    GetTailscaleStatus = { param($TargetAddress) Get-CloudTailscaleStatus -TargetAddress $TargetAddress }
    ResolveDnsName = { param($Name) Resolve-CloudDnsName -Name $Name }
    TestHostsOverride = { param($Domain) Test-CloudHostsOverride -Domain $Domain }
    GetDesktopPath = { Get-CloudDesktopPath }
    NewShortcutShell = { New-CloudShortcutShell }
}

function Get-CloudConsoleAdapter {
    param($Adapter)
    $merged = @{}
    foreach ($key in $script:CloudConsoleDefaultAdapter.Keys) {
        $merged[$key] = $script:CloudConsoleDefaultAdapter[$key]
    }
    if ($null -ne $Adapter) {
        foreach ($key in $Adapter.Keys) {
            if ($null -ne $Adapter[$key]) { $merged[$key] = $Adapter[$key] }
        }
    }
    return $merged
}

function Test-CloudTunnelCommandLine {
    param([string]$CommandLine, [string]$ConfigPath, [string]$HostAlias, [string]$Route)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    if ($Route -ne 'private' -and $Route -ne 'public') { return $false }
    if ([string]::IsNullOrWhiteSpace($ConfigPath) -or [string]::IsNullOrWhiteSpace($HostAlias)) { return $false }
    $normalized = $CommandLine.Replace('"', '').Replace("'", '').Replace('/', '\')
    $config = $ConfigPath.Replace('/', '\')
    $forward = '{0}:{1}:{2}:{1}' -f $script:CloudConsoleBind, $script:CloudConsolePort, $script:CloudConsoleBind
    $bareForward = '{0}:{1}:{2}' -f $script:CloudConsolePort, $script:CloudConsoleBind, $script:CloudConsolePort
    $options = [Text.RegularExpressions.RegexOptions]::IgnoreCase
    if (-not [regex]::IsMatch($normalized, '(?:^|\s)-F\s*' + [regex]::Escape($config) + '(?:\s|$)', $options)) { return $false }
    if (-not [regex]::IsMatch($normalized, '(?:^|\s)-N(?:\s|$)', $options)) { return $false }
    if (-not ([regex]::IsMatch($normalized, '(?:^|\s)-L\s*' + [regex]::Escape($forward) + '(?:\s|$)', $options) -or
            [regex]::IsMatch($normalized, '(?:^|\s)-L\s*' + [regex]::Escape($bareForward) + '(?:\s|$)', $options))) {
        return $false
    }
    if (-not [regex]::IsMatch($normalized, '(?:^|\s)' + [regex]::Escape($HostAlias) + '(?:\s|$)', $options)) { return $false }
    return $true
}

function Get-CloudTunnelOwnership {
    param($Adapter, [string]$ConfigPath, [string]$HostAlias, [string]$Route)
    $listeners = @()
    try { $listeners = @(& $Adapter.GetListeners $script:CloudConsolePort) }
    catch {
        return @{ State = 'Unconfirmed'; Pid = 0; Detail = "端口查询失败: $($_.Exception.Message)" }
    }
    $listeners = @($listeners | Where-Object { $null -ne $_ })
    if ($listeners.Count -eq 0) { return @{ State = 'None'; Pid = 0; Detail = '无监听' } }
    $owned = @()
    $foreign = @()
    $unconfirmed = @()
    foreach ($listener in $listeners) {
        $address = [string](Get-CloudObjectProperty -Object $listener -Name 'LocalAddress')
        $ownerPid = 0
        $ownerPidValue = Get-CloudObjectProperty -Object $listener -Name 'OwningProcess'
        $ownerPidParsed = [int]::TryParse([string]$ownerPidValue, [ref]$ownerPid)
        if ([string]::IsNullOrWhiteSpace($address) -or -not $ownerPidParsed -or $ownerPid -le 0) {
            $unconfirmed += '监听地址或进程不可读'
            continue
        }
        $ip = $null
        if (-not [Net.IPAddress]::TryParse($address, [ref]$ip)) {
            $unconfirmed += "监听地址不是 IP: $address"
            continue
        }
        if ($ip.IsIPv4MappedToIPv6) { $ip = $ip.MapToIPv4() }
        if (-not $ip.Equals([Net.IPAddress]::Loopback)) {
            $foreign += "非 loopback 监听 $address"
            continue
        }
        $process = $null
        try { $process = & $Adapter.GetProcess $ownerPid }
        catch { $unconfirmed += "进程查询失败 pid=$ownerPid"; continue }
        if ($null -eq $process -or -not [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) {
            $unconfirmed += "监听进程不存在 pid=$ownerPid"
            continue
        }
        $processName = [string](Get-CloudObjectProperty -Object $process -Name 'Name')
        $baseName = [IO.Path]::GetFileNameWithoutExtension($processName)
        if ($baseName -ine 'ssh') {
            $foreign += "监听进程不是 ssh.exe: $processName"
            continue
        }
        $commandLine = [string](Get-CloudObjectProperty -Object $process -Name 'CommandLine')
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            $unconfirmed += "ssh 命令行不可读 pid=$ownerPid"
            continue
        }
        if (-not (Test-CloudTunnelCommandLine -CommandLine $commandLine -ConfigPath $ConfigPath -HostAlias $HostAlias -Route $Route)) {
            $foreign += "ssh 命令行不匹配本入口${Route}隧道 pid=$ownerPid"
            continue
        }
        if (-not $owned.Contains($ownerPid)) { $owned += $ownerPid }
    }
    if ($foreign.Count -gt 0) {
        return @{ State = 'Foreign'; Pid = 0; Detail = ($foreign -join '; ') }
    }
    if ($unconfirmed.Count -gt 0) {
        return @{ State = 'Unconfirmed'; Pid = 0; Detail = ($unconfirmed -join '; ') }
    }
    if ($owned.Count -eq 1) {
        return @{ State = 'Owned'; Pid = [int]$owned[0]; Detail = '' }
    }
    return @{ State = 'Unconfirmed'; Pid = 0; Detail = '多个匹配的监听进程' }
}

function Get-CloudConsoleOwnedTunnel {
    param($Adapter, $Routes)
    $owned = @()
    $foreign = @()
    $unconfirmed = @()
    foreach ($route in @($Routes)) {
        $ownership = Get-CloudTunnelOwnership -Adapter $Adapter -ConfigPath $route.ConfigPath `
            -HostAlias $route.HostAlias -Route $route.Name
        switch ($ownership.State) {
            'Owned' { $owned += @{ Route = $route; Pid = [int]$ownership.Pid } }
            'Foreign' { $foreign += "$($route.Name): $($ownership.Detail)" }
            'Unconfirmed' { $unconfirmed += "$($route.Name): $($ownership.Detail)" }
        }
    }
    if ($owned.Count -eq 1) {
        return @{ State = 'Owned'; Route = $owned[0].Route; Pid = [int]$owned[0].Pid; Detail = '' }
    }
    if ($owned.Count -gt 1) {
        return @{ State = 'Unconfirmed'; Route = $null; Pid = 0; Detail = '多个路由的隧道同时监听 18096' }
    }
    if ($unconfirmed.Count -gt 0) {
        return @{ State = 'Unconfirmed'; Route = $null; Pid = 0; Detail = ($unconfirmed -join '; ') }
    }
    if ($foreign.Count -gt 0) {
        return @{ State = 'Foreign'; Route = $null; Pid = 0; Detail = ($foreign -join '; ') }
    }
    return @{ State = 'None'; Route = $null; Pid = 0; Detail = '无监听' }
}

function Resolve-CloudConsoleStateContext {
    param($Adapter, $State, $Routes, [string]$DefaultRoute = 'private')
    $defaultConfig = ''
    $defaultHost = ''
    $defaultRouteDefinition = Get-CloudConsoleRoute -Routes $Routes -Name $DefaultRoute
    if ($null -ne $defaultRouteDefinition) {
        $defaultConfig = [string]$defaultRouteDefinition.ConfigPath
        $defaultHost = [string]$defaultRouteDefinition.HostAlias
    }
    if ($null -eq $State) {
        return @{ State = $null; ConfigPath = $defaultConfig; HostAlias = $defaultHost; Route = $DefaultRoute;
            Process = $null; Pid = 0; Alive = $false; Matches = $false; Error = '' }
    }
    $statePid = 0
    $pidValue = Get-CloudObjectProperty -Object $State -Name 'pid'
    $parsed = [int]::TryParse([string]$pidValue, [ref]$statePid)
    if (-not $parsed -or $statePid -le 0) {
        return @{ State = $State; ConfigPath = $defaultConfig; HostAlias = $defaultHost; Route = $DefaultRoute;
            Process = $null; Pid = 0; Alive = $false; Matches = $false; Error = '状态文件 pid 无效' }
    }
    $process = $null
    try { $process = & $Adapter.GetProcess $statePid }
    catch {
        return @{ State = $State; ConfigPath = $defaultConfig; HostAlias = $defaultHost; Route = $DefaultRoute;
            Process = $null; Pid = $statePid; Alive = $null; Matches = $false; Error = $_.Exception.Message }
    }
    if ($null -eq $process -or -not [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) {
        return @{ State = $State; ConfigPath = $defaultConfig; HostAlias = $defaultHost; Route = $DefaultRoute;
            Process = $null; Pid = $statePid; Alive = $false; Matches = $false; Error = '' }
    }
    $stateConfig = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_config')
    $stateHost = [string](Get-CloudObjectProperty -Object $State -Name 'ssh_host')
    $stateRoute = Get-CloudConsoleStateRoute -State $State -Routes $Routes
    if ([string]::IsNullOrWhiteSpace($stateRoute)) {
        if ([string]::IsNullOrWhiteSpace($stateConfig) -or [string]::IsNullOrWhiteSpace($stateHost)) {
            $stateRoute = $DefaultRoute
            $stateConfig = $defaultConfig
            $stateHost = $defaultHost
        } else {
            return @{ State = $State; ConfigPath = $stateConfig; HostAlias = $stateHost; Route = '';
                Process = $process; Pid = $statePid; Alive = $true; Matches = $false; Error = '' }
        }
    }
    $commandLine = [string](Get-CloudObjectProperty -Object $process -Name 'CommandLine')
    $commandMatches = Test-CloudTunnelCommandLine -CommandLine $commandLine -ConfigPath $stateConfig `
        -HostAlias $stateHost -Route $stateRoute
    return @{ State = $State; ConfigPath = $stateConfig; HostAlias = $stateHost; Route = $stateRoute;
        Process = $process; Pid = $statePid; Alive = $true; Matches = $commandMatches; Error = '' }
}

function Get-CloudTunnelArgumentLine {
    param([string]$ConfigPath, [string]$HostAlias)
    $quotedConfig = '"' + $ConfigPath.Replace('"', '\"') + '"'
    $forward = '{0}:{1}:{2}:{1}' -f $script:CloudConsoleBind, $script:CloudConsolePort, $script:CloudConsoleBind
    $template = '-F {0} -N -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes ' +
        '-o ExitOnForwardFailure=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 ' +
        '-o ServerAliveCountMax=3 -o TCPKeepAlive=yes -L {1} {2}'
    return ($template -f $quotedConfig, $forward, $HostAlias)
}

function Invoke-CloudConsoleHealthCheck {
    param($Adapter, [string]$Uri, [int]$TimeoutSeconds)
    $response = $null
    try { $response = & $Adapter.TestHealth $Uri $TimeoutSeconds }
    catch { return @{ Ok = $false; Detail = "健康检查请求失败: $($_.Exception.Message)" } }
    if ($null -eq $response) { return @{ Ok = $false; Detail = '健康检查无响应' } }
    if (-not [bool](Get-CloudObjectProperty -Object $response -Name 'Ok')) {
        $detail = [string](Get-CloudObjectProperty -Object $response -Name 'Detail')
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = '健康检查请求失败' }
        return @{ Ok = $false; Detail = $detail }
    }
    $statusCode = 0
    try { $statusCode = [int](Get-CloudObjectProperty -Object $response -Name 'StatusCode') }
    catch { $statusCode = 0 }
    if ($statusCode -ne 200) { return @{ Ok = $false; Detail = "健康检查 HTTP $statusCode" } }
    $content = [string](Get-CloudObjectProperty -Object $response -Name 'Content')
    $payload = $null
    try { $payload = $content | ConvertFrom-Json }
    catch { return @{ Ok = $false; Detail = '健康检查响应不是 JSON' } }
    $status = [string](Get-CloudObjectProperty -Object $payload -Name 'status')
    $service = [string](Get-CloudObjectProperty -Object $payload -Name 'service')
    if ($status -ne 'ok') { return @{ Ok = $false; Detail = "健康检查状态不是 ok: $status" } }
    if ($service -ne 'usagesrv') { return @{ Ok = $false; Detail = "健康检查服务不是 usagesrv: $service" } }
    $port = Get-CloudObjectProperty -Object $payload -Name 'port'
    if ($null -ne $port) {
        $portNumber = 0
        if (-not [int]::TryParse([string]$port, [ref]$portNumber) -or
                $portNumber -ne $script:CloudConsolePort) {
            return @{ Ok = $false; Detail = "健康检查端口不是 $($script:CloudConsolePort): $port" }
        }
    }
    return @{ Ok = $true; Detail = '健康检查通过' }
}

function Get-CloudTunnelFailureHint {
    param([string]$StderrPath, [string]$Route)
    if ([string]::IsNullOrWhiteSpace($StderrPath) -or -not (Test-Path -LiteralPath $StderrPath -PathType Leaf)) {
        return 'SSH 隧道未就绪，详见 stderr 日志'
    }
    $tail = ''
    try { $tail = (Get-Content -LiteralPath $StderrPath -Tail 20 -ErrorAction Stop) -join ' ' }
    catch { return 'SSH 隧道未就绪，详见 stderr 日志' }
    if ($tail -match '(?i)permission denied|publickey') {
        return 'SSH 认证失败：请确认原密钥和专用 known_hosts 仍匹配'
    }
    if ($tail -match '(?i)host key verification failed') {
        return '主机密钥校验失败：请核对专用 known_hosts'
    }
    if ($tail -match '(?i)remote port forwarding failed|cannot listen|address already in use') {
        return '本地 18096 端口无法绑定，可能被其他程序占用'
    }
    if ($tail -match '(?i)could not resolve|connection timed out|no route to host|network is unreachable') {
        if ($Route -eq 'public') {
            return '无法连接云机公网 SSH：请确认云机公网 22 端口与安全组管理源可达，本入口不会修改安全组或 SSH 信任'
        }
        return '无法连接云机：请确认 Tailscale 已运行、两端在线，本入口不会自动启动 Tailscale 或本地代理'
    }
    return 'SSH 隧道未就绪，详见 stderr 日志'
}

function Wait-CloudConsoleHealthy {
    param($Adapter, [int]$ProcessId, [string]$ConfigPath, [string]$HostAlias, [string]$Route,
          [int]$HealthTimeoutSeconds, [int]$HealthPollMilliseconds, [string]$StderrPath)
    $attempts = [Math]::Max(1, [int][Math]::Ceiling(($HealthTimeoutSeconds * 1000.0) / [Math]::Max(1, $HealthPollMilliseconds)))
    $lastHealthDetail = '健康检查未通过'
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $ownership = Get-CloudTunnelOwnership -Adapter $Adapter -ConfigPath $ConfigPath -HostAlias $HostAlias -Route $Route
        if ($ownership.State -eq 'Foreign') {
            return @{ Ok = $false; ExitCode = 3; ProcessExited = $false; Event = 'open.foreign'; Pid = 0;
                CommandLine = ''; SshExe = ''; Label = '端口'
                Message = "18096 已被其他程序占用（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。" }
        }
        if ($ownership.State -eq 'Unconfirmed') {
            return @{ Ok = $false; ExitCode = 3; ProcessExited = $false; Event = 'open.unconfirmed'; Pid = 0;
                CommandLine = ''; SshExe = ''; Label = '端口'
                Message = "18096 有监听但无法确认归属（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。" }
        }
        if ($ownership.State -eq 'None') {
            $process = $null
            try { $process = & $Adapter.GetProcess $ProcessId }
            catch { $process = $null }
            if ($null -eq $process -or -not [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) {
                $hint = Get-CloudTunnelFailureHint -StderrPath $StderrPath -Route $Route
                return @{ Ok = $false; ExitCode = 1; ProcessExited = $true; Event = 'open.exited'; Pid = 0;
                    CommandLine = ''; SshExe = ''; Label = '错误'
                    Message = "SSH 隧道进程已退出（$hint）。" }
            }
        } else {
            $health = Invoke-CloudConsoleHealthCheck -Adapter $Adapter -Uri $script:CloudConsoleHealthUrl `
                -TimeoutSeconds ([Math]::Min(5, [Math]::Max(1, $HealthTimeoutSeconds)))
            if ($health.Ok) {
                $process = $null
                try { $process = & $Adapter.GetProcess $ownership.Pid }
                catch { $process = $null }
                $commandLine = [string](Get-CloudObjectProperty -Object $process -Name 'CommandLine')
                $sshExe = [string](Get-CloudObjectProperty -Object $process -Name 'Name')
                return @{ Ok = $true; ExitCode = 0; ProcessExited = $false; Event = 'open.ready'; Pid = $ownership.Pid;
                    CommandLine = $commandLine; SshExe = $sshExe; Label = '入口'
                    Message = 'UM 云端控制台隧道已就绪。' }
            }
            $lastHealthDetail = [string]$health.Detail
        }
        if ($attempt -lt ($attempts - 1)) { & $Adapter.Sleep $HealthPollMilliseconds }
    }
    $process = $null
    try { $process = & $Adapter.GetProcess $ProcessId }
    catch { $process = $null }
    if ($null -eq $process -or -not [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) {
        $hint = Get-CloudTunnelFailureHint -StderrPath $StderrPath -Route $Route
        return @{ Ok = $false; ExitCode = 1; ProcessExited = $true; Event = 'open.exited'; Pid = 0;
            CommandLine = ''; SshExe = ''; Label = '错误'
            Message = "SSH 隧道进程已退出（$hint）。" }
    }
    $hint = Get-CloudTunnelFailureHint -StderrPath $StderrPath -Route $Route
    return @{ Ok = $false; ExitCode = 4; ProcessExited = $false; Event = 'open.timeout'; Pid = 0;
        CommandLine = ''; SshExe = ''; Label = '健康'
        Message = "自建隧道在 $HealthTimeoutSeconds 秒内未通过健康检查（$lastHealthDetail；$hint）；进程保留，可重试或 -Stop。" }
}

function Open-CloudConsolePage {
    param($Adapter, [string]$Url)
    try { $opened = & $Adapter.OpenBrowser $Url }
    catch { return @{ Ok = $false; Detail = $_.Exception.Message } }
    if (-not [bool]$opened) { return @{ Ok = $false; Detail = '浏览器启动返回失败' } }
    return @{ Ok = $true; Detail = '' }
}

function Read-CloudConsoleState {
    param([string]$StatePath)
    if ([string]::IsNullOrWhiteSpace($StatePath)) { return $null }
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Write-CloudConsoleState {
    param([string]$StatePath, [int]$ProcessId, [string]$CommandLine,
          [string]$SshExe, [string]$ConfigPath, [string]$HostAlias, [string]$Route)
    if ([string]::IsNullOrWhiteSpace($StatePath)) { throw '状态文件路径为空' }
    $payload = [ordered]@{
        pid = [int]$ProcessId
        started_utc = [DateTime]::UtcNow.ToString('o')
        bind = $script:CloudConsoleBind
        port = $script:CloudConsolePort
        target = ('{0}:{1}' -f $script:CloudConsoleBind, $script:CloudConsoleTargetPort)
        route = $Route
        ssh_exe = $SshExe
        ssh_config = $ConfigPath
        ssh_host = $HostAlias
        command_line = $CommandLine
    } | ConvertTo-Json -Depth 4
    $directory = Split-Path -Parent $StatePath
    if ($directory -and -not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $temp = $StatePath + '.tmp.' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temp, $payload, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temp -Destination $StatePath -Force
    } catch {
        if (Test-Path -LiteralPath $temp -PathType Leaf) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Remove-CloudConsoleState {
    param([string]$StatePath)
    if ([string]::IsNullOrWhiteSpace($StatePath)) { return $true }
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $true }
    try {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-CloudConsoleSshPreflight {
    param($Adapter, [string]$SshConfig, [string]$SshHostAlias, [bool]$RequireTailscaleAddress = $true)
    if ($SshHostAlias -notmatch '\A[A-Za-z0-9._-]+\z') {
        return New-CloudResult 2 '预检' 'open.config' 'SSH 主机别名非法。'
    }
    if ([string]::IsNullOrWhiteSpace($SshConfig)) {
        return New-CloudResult 2 '预检' 'open.config' 'SSH 配置路径为空。'
    }
    $sshExe = $null
    try { $sshExe = [string](& $Adapter.ResolveSsh) }
    catch { return New-CloudResult 2 '预检' 'open.config' "未找到 ssh.exe：$($_.Exception.Message)" }
    if ([string]::IsNullOrWhiteSpace($sshExe)) {
        return New-CloudResult 2 '预检' 'open.config' 'ssh.exe 路径为空。'
    }
    $configExists = $false
    try { $configExists = [bool](& $Adapter.TestPath $SshConfig) }
    catch { return New-CloudResult 2 '预检' 'open.config' "无法检查 SSH 配置：$($_.Exception.Message)" }
    if (-not $configExists) {
        return New-CloudResult 2 '预检' 'open.config' "SSH 配置不存在：$SshConfig"
    }
    $effective = $null
    try { $effective = & $Adapter.GetEffectiveConfig $sshExe $SshConfig $SshHostAlias }
    catch { return New-CloudResult 2 '预检' 'open.config' "ssh -G 失败：$($_.Exception.Message)" }
    $configResult = Assert-CloudSshEffectiveConfig -Adapter $Adapter -Effective $effective `
        -ConfigPath $SshConfig -RequireTailscaleAddress $RequireTailscaleAddress
    if (-not $configResult.Ok) {
        return New-CloudResult 2 '预检' 'open.config' ('SSH 配置预检失败：' + ($configResult.Problems -join '；'))
    }
    return @{ ExitCode = 0; SshExe = $sshExe; Effective = $effective }
}

function New-CloudRouteResult {
    param([string]$Route, [bool]$Ok, [bool]$CanFallback, [string]$Stage, [int]$ExitCode,
          [string]$Label, [string]$Event, [string]$Message)
    return @{
        Route = $Route
        Ok = $Ok
        CanFallback = $CanFallback
        Stage = $Stage
        ExitCode = $ExitCode
        Label = $Label
        Event = $Event
        Message = $Message
    }
}

function Invoke-CloudConsoleRouteOpen {
    param($Adapter, $Route, $Routes, [string]$StatePath, [string]$LogPath,
          [int]$HealthTimeoutSeconds, [int]$HealthPollMilliseconds,
          [string]$StdoutPath, [string]$StderrPath)
    $routeName = [string]$Route.Name
    $preflight = Get-CloudConsoleSshPreflight -Adapter $Adapter -SshConfig $Route.ConfigPath `
        -SshHostAlias $Route.HostAlias -RequireTailscaleAddress ([bool]$Route.RequireTailscaleAddress)
    if ($preflight.ExitCode -ne 0) {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $true -Stage 'preflight' `
            -ExitCode $preflight.ExitCode -Label $preflight.Label -Event $preflight.Event -Message $preflight.Message
    }

    $state = Read-CloudConsoleState -StatePath $StatePath
    if ($null -eq $state -and (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        [void](Remove-CloudConsoleState -StatePath $StatePath)
        Write-CloudConsoleLog -Level 'WARN' -Event 'open.stale' -Message '状态文件不可读，已清理。' -LogPath $LogPath
    }
    $context = Resolve-CloudConsoleStateContext -Adapter $Adapter -State $state -Routes $Routes `
        -DefaultRoute $routeName
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $null) {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
            -ExitCode 1 -Label '错误' -Event 'open.unconfirmed' `
            -Message "状态文件记录的进程无法确认（$($context.Error)）；未启动新隧道。"
    }
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $true -and -not $context.Matches) {
        [void](Remove-CloudConsoleState -StatePath $StatePath)
        Write-CloudConsoleLog -Level 'WARN' -Event 'open.stale' -Message '状态文件指向的进程不是本入口隧道，已清理过期状态。' -LogPath $LogPath
        $context = $null
    } elseif ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $false) {
        [void](Remove-CloudConsoleState -StatePath $StatePath)
        Write-CloudConsoleLog -Level 'INFO' -Event 'open.stale' -Message '状态文件进程已退出，已清理过期状态。' -LogPath $LogPath
        $context = $null
    }
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $true -and
            $context.Matches -and $context.Route -eq $routeName) {
        $wait = Wait-CloudConsoleHealthy -Adapter $Adapter -ProcessId $context.Pid `
            -ConfigPath $context.ConfigPath -HostAlias $context.HostAlias -Route $routeName `
            -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
            -StderrPath $StderrPath
        if (-not $wait.Ok) {
            if ($wait.ExitCode -eq 1) { [void](Remove-CloudConsoleState -StatePath $StatePath) }
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback ($wait.ExitCode -eq 1) `
                -Stage 'wait' -ExitCode $wait.ExitCode -Label $wait.Label -Event $wait.Event -Message $wait.Message
        }
        try {
            Write-CloudConsoleState -StatePath $StatePath -ProcessId $wait.Pid -CommandLine $wait.CommandLine `
                -SshExe $wait.SshExe -ConfigPath $context.ConfigPath -HostAlias $context.HostAlias -Route $routeName
        } catch {
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
                -ExitCode 1 -Label '错误' -Event 'open.statefailed' `
                -Message "隧道已就绪，但写入状态文件失败：$($_.Exception.Message)"
        }
        Write-CloudConsoleLog -Level 'INFO' -Event 'open.reuse' -Message "route=$routeName pid=$($wait.Pid)" -LogPath $LogPath
        $auth = Open-CloudConsoleAuthenticatedPage -Adapter $Adapter -SshPreflight $preflight `
            -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias
        return New-CloudRouteResult -Route $routeName -Ok ($auth.ExitCode -eq 0) -CanFallback $false `
            -Stage 'authorization' -ExitCode $auth.ExitCode -Label $auth.Label -Event $auth.Event `
            -Message ('[' + $routeName + '] ' + $auth.Message)
    }

    $ownership = Get-CloudTunnelOwnership -Adapter $Adapter -ConfigPath $Route.ConfigPath `
        -HostAlias $Route.HostAlias -Route $routeName
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $true -and
            $context.Matches -and $context.Route -ne $routeName -and $ownership.State -eq 'None') {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
            -ExitCode 4 -Label '健康' -Event 'open.unhealthy' `
            -Message "状态文件记录的$($context.Route)隧道进程在（pid $($context.Pid)），但 18096 无监听；未启动新隧道。"
    }
    if ($ownership.State -eq 'Foreign') {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'ownership' `
            -ExitCode 3 -Label '端口' -Event 'open.foreign' `
            -Message "18096 被其他程序占用（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。"
    }
    if ($ownership.State -eq 'Unconfirmed') {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'ownership' `
            -ExitCode 3 -Label '端口' -Event 'open.unconfirmed' `
            -Message "18096 监听归属无法确认（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。"
    }
    if ($ownership.State -eq 'Owned') {
        $process = $null
        try { $process = & $Adapter.GetProcess $ownership.Pid }
        catch { $process = $null }
        $commandLine = [string](Get-CloudObjectProperty -Object $process -Name 'CommandLine')
        $sshExe = [string](Get-CloudObjectProperty -Object $process -Name 'Name')
        try {
            Write-CloudConsoleState -StatePath $StatePath -ProcessId $ownership.Pid -CommandLine $commandLine `
                -SshExe $sshExe -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias -Route $routeName
        } catch {
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
                -ExitCode 1 -Label '错误' -Event 'open.statefailed' `
                -Message "复用隧道成功，但写入状态文件失败：$($_.Exception.Message)"
        }
        $wait = Wait-CloudConsoleHealthy -Adapter $Adapter -ProcessId $ownership.Pid `
            -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias -Route $routeName `
            -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
            -StderrPath $StderrPath
        if (-not $wait.Ok) {
            if ($wait.ExitCode -eq 1) { [void](Remove-CloudConsoleState -StatePath $StatePath) }
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback ($wait.ExitCode -eq 1) `
                -Stage 'wait' -ExitCode $wait.ExitCode -Label $wait.Label -Event $wait.Event -Message $wait.Message
        }
        Write-CloudConsoleLog -Level 'INFO' -Event 'open.reuse' -Message "route=$routeName pid=$($ownership.Pid)" -LogPath $LogPath
        $auth = Open-CloudConsoleAuthenticatedPage -Adapter $Adapter -SshPreflight $preflight `
            -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias
        return New-CloudRouteResult -Route $routeName -Ok ($auth.ExitCode -eq 0) -CanFallback $false `
            -Stage 'authorization' -ExitCode $auth.ExitCode -Label $auth.Label -Event $auth.Event `
            -Message ('[' + $routeName + '] ' + $auth.Message)
    }

    if ([bool]$Route.RequireTailscaleAddress) {
        $effective = $preflight.Effective
        $targetAddress = Get-CloudEffectiveValue -Effective $effective -Key 'hostname'
        $tailscale = Get-CloudTailscalePreflight -Adapter $Adapter -TargetAddress $targetAddress
        if (-not $tailscale.Ok) {
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $true -Stage 'preflight' `
                -ExitCode 2 -Label '预检' -Event 'open.tailscale' `
                -Message "Tailscale 预检失败：$($tailscale.Detail)"
        }
        if ($tailscale.Detail -match '继续尝试') {
            Write-CloudConsoleLog -Level 'WARN' -Event 'open.tailscale' -Message $tailscale.Detail -LogPath $LogPath
        }
    }

    $sshExe = $preflight.SshExe
    $argumentLine = Get-CloudTunnelArgumentLine -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias
    $tunnel = $null
    try { $tunnel = & $Adapter.StartTunnel $sshExe $argumentLine $StdoutPath $StderrPath }
    catch {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $true -Stage 'start' `
            -ExitCode 1 -Label '错误' -Event 'open.startfailed' `
            -Message "SSH 隧道启动失败：$($_.Exception.Message)"
    }
    $processId = 0
    $processIdValue = Get-CloudObjectProperty -Object $tunnel -Name 'ProcessId'
    if ($null -eq $processIdValue) { $processIdValue = Get-CloudObjectProperty -Object $tunnel -Name 'Id' }
    if (-not [int]::TryParse([string]$processIdValue, [ref]$processId) -or $processId -le 0) {
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $true -Stage 'start' `
            -ExitCode 1 -Label '错误' -Event 'open.startfailed' `
            -Message 'SSH 隧道启动失败：没有返回有效进程 ID。'
    }
    try {
        Write-CloudConsoleState -StatePath $StatePath -ProcessId $processId -CommandLine $argumentLine `
            -SshExe $sshExe -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias -Route $routeName
    } catch {
        [void](& $Adapter.StopProcess $processId)
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
            -ExitCode 1 -Label '错误' -Event 'open.statefailed' `
            -Message "写入状态文件失败，已尝试停止新隧道：$($_.Exception.Message)"
    }
    Write-CloudConsoleLog -Level 'INFO' -Event 'open.started' -Message "route=$routeName pid=$processId" -LogPath $LogPath
    $wait = Wait-CloudConsoleHealthy -Adapter $Adapter -ProcessId $processId `
        -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias -Route $routeName `
        -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
        -StderrPath $StderrPath
    if (-not $wait.Ok) {
        if ($wait.ExitCode -eq 1) { [void](Remove-CloudConsoleState -StatePath $StatePath) }
        return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback ($wait.ExitCode -eq 1) `
            -Stage 'wait' -ExitCode $wait.ExitCode -Label $wait.Label -Event $wait.Event -Message $wait.Message
    }
    if ($wait.Pid -ne $processId) {
        try {
            Write-CloudConsoleState -StatePath $StatePath -ProcessId $wait.Pid -CommandLine $wait.CommandLine `
                -SshExe $wait.SshExe -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias -Route $routeName
        } catch {
            return New-CloudRouteResult -Route $routeName -Ok $false -CanFallback $false -Stage 'state' `
                -ExitCode 1 -Label '错误' -Event 'open.statefailed' `
                -Message "隧道已就绪，但写入状态文件失败：$($_.Exception.Message)"
        }
    }
    $auth = Open-CloudConsoleAuthenticatedPage -Adapter $Adapter -SshPreflight $preflight `
        -ConfigPath $Route.ConfigPath -HostAlias $Route.HostAlias
    return New-CloudRouteResult -Route $routeName -Ok ($auth.ExitCode -eq 0) -CanFallback $false `
        -Stage 'authorization' -ExitCode $auth.ExitCode -Label $auth.Label -Event $auth.Event -Message $auth.Message
}

function Get-CloudConsoleCombinedExitCode {
    param($Private, $Public)
    if ($Private.Stage -ne 'preflight') { return [int]$Private.ExitCode }
    if ($Public.Stage -ne 'preflight') { return [int]$Public.ExitCode }
    return 2
}

function Invoke-CloudConsoleOpen {
    param($Adapter, $Routes, [string]$StatePath, [string]$LogPath,
          [int]$HealthTimeoutSeconds, [int]$HealthPollMilliseconds)
    $stateDirectory = Split-Path -Parent $StatePath
    if ([string]::IsNullOrWhiteSpace($stateDirectory)) { $stateDirectory = '.' }
    $stdoutPath = Join-Path $stateDirectory 'cloud-console-tunnel.stdout.log'
    $stderrPath = Join-Path $stateDirectory 'cloud-console-tunnel.stderr.log'

    $privateRoute = Get-CloudConsoleRoute -Routes $Routes -Name 'private'
    $private = Invoke-CloudConsoleRouteOpen -Adapter $Adapter -Route $privateRoute -Routes $Routes `
        -StatePath $StatePath -LogPath $LogPath `
        -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
        -StdoutPath $stdoutPath -StderrPath $stderrPath
    if ($private.Ok) { return New-CloudResult 0 $private.Label $private.Event $private.Message }
    Write-CloudConsoleLog -Level 'WARN' -Event $private.Event `
        -Message ('private: ' + $private.Message) -LogPath $LogPath
    if (-not $private.CanFallback) {
        return New-CloudResult $private.ExitCode $private.Label $private.Event $private.Message
    }

    $publicRoute = Get-CloudConsoleRoute -Routes $Routes -Name 'public'
    $public = Invoke-CloudConsoleRouteOpen -Adapter $Adapter -Route $publicRoute -Routes $Routes `
        -StatePath $StatePath -LogPath $LogPath `
        -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
        -StdoutPath $stdoutPath -StderrPath $stderrPath
    if ($public.Ok) {
        Write-CloudConsoleLog -Level 'INFO' -Event 'open.fallback' `
            -Message "private: $($private.Message) -> public: $($public.Message)" -LogPath $LogPath
        return New-CloudResult 0 $public.Label $public.Event $public.Message
    }
    Write-CloudConsoleLog -Level 'WARN' -Event $public.Event `
        -Message ('public: ' + $public.Message) -LogPath $LogPath
    $exitCode = Get-CloudConsoleCombinedExitCode -Private $private -Public $public
    $message = "私网与公网 SSH 隧道均失败。private: $($private.Message)；public: $($public.Message)"
    Write-CloudConsoleLog -Level 'WARN' -Event 'open.failed' -Message $message -LogPath $LogPath
    return New-CloudResult $exitCode '错误' 'open.failed' $message
}

function Get-CloudConsoleCheckDiagnostics {
    param($Adapter, $Routes)
    $lines = [System.Collections.Generic.List[string]]::new()
    $preflights = @{}
    foreach ($route in @($Routes)) {
        $preflight = Get-CloudConsoleSshPreflight -Adapter $Adapter -SshConfig $route.ConfigPath `
            -SshHostAlias $route.HostAlias -RequireTailscaleAddress ([bool]$route.RequireTailscaleAddress)
        $preflights[[string]$route.Name] = $preflight
        if ($preflight.ExitCode -eq 0) {
            $lines.Add(('[{0}] SSH 预检通过：{1} ({2})' -f $route.Name, $route.ConfigPath, $route.HostAlias))
        } else {
            $lines.Add(('[{0}] SSH 预检失败：{1}' -f $route.Name, $preflight.Message))
        }
    }
    $selected = 'none'
    if ($preflights.ContainsKey('private') -and $preflights['private'].ExitCode -eq 0) {
        $selected = 'private'
    } elseif ($preflights.ContainsKey('public') -and $preflights['public'].ExitCode -eq 0) {
        $selected = 'public'
    }
    $lines.Add(('选路：{0}（私网优先，公网回退）' -f $selected))

    $domain = $script:CloudConsolePublicDomain
    $dns = $null
    try { $dns = & $Adapter.ResolveDnsName $domain }
    catch { $dns = @{ Ok = $false; Addresses = @(); Detail = $_.Exception.Message } }
    if ($null -eq $dns) { $dns = @{ Ok = $false; Addresses = @(); Detail = '无结果' } }
    if ([bool](Get-CloudObjectProperty -Object $dns -Name 'Ok')) {
        $addresses = @(Get-CloudObjectProperty -Object $dns -Name 'Addresses' |
            Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        if ($addresses.Count -gt 0) {
            $lines.Add(('Resolve-DnsName {0}: {1}' -f $domain, (($addresses | ForEach-Object { [string]$_ }) -join ', ')))
        } else {
            $lines.Add(('Resolve-DnsName {0}: 无 A 记录' -f $domain))
        }
    } else {
        $detail = [string](Get-CloudObjectProperty -Object $dns -Name 'Detail')
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = '解析失败' }
        $lines.Add(('Resolve-DnsName {0} 失败：{1}' -f $domain, $detail))
    }

    $hosts = $null
    try { $hosts = & $Adapter.TestHostsOverride $domain }
    catch { $hosts = @{ Exists = $false; Override = $false; Detail = $_.Exception.Message } }
    if ($null -eq $hosts) { $hosts = @{ Exists = $false; Override = $false; Detail = '无结果' } }
    $hostsExists = [bool](Get-CloudObjectProperty -Object $hosts -Name 'Exists')
    $hostsOverride = [bool](Get-CloudObjectProperty -Object $hosts -Name 'Override')
    if (-not $hostsExists) {
        $detail = [string](Get-CloudObjectProperty -Object $hosts -Name 'Detail')
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = 'hosts 文件不存在或不可读' }
        $lines.Add(('hosts 覆盖：{0}；本入口不依赖 hosts 覆盖。' -f $detail))
    } elseif ($hostsOverride) {
        $lines.Add('hosts 覆盖：检测到该域名的 hosts 条目；本入口不使用 hosts 覆盖，公网 SSH 使用专用配置的 HostName。')
    } else {
        $lines.Add('hosts 覆盖：未发现该域名的 hosts 条目；本入口不需要 hosts 覆盖。')
    }
    $lines.Add('结论：本入口不需要旧 fix 的固定 IP/hosts 覆盖；公网 SSH 使用专用配置的主机名，浏览器仍访问 localhost:18096。')
    return @{ Selected = $selected; Preflights = $preflights; Dns = $dns; Hosts = $hosts; Lines = @($lines) }
}

function Invoke-CloudConsoleCheck {
    param($Adapter, $Routes, [string]$StatePath, [string]$LogPath)
    $diagnostics = Get-CloudConsoleCheckDiagnostics -Adapter $Adapter -Routes $Routes
    foreach ($line in $diagnostics.Lines) { Write-Host $line }
    Write-CloudConsoleLog -Level 'INFO' -Event 'check.route' -Message ('selected=' + $diagnostics.Selected) -LogPath $LogPath

    $state = Read-CloudConsoleState -StatePath $StatePath
    $context = Resolve-CloudConsoleStateContext -Adapter $Adapter -State $state -Routes $Routes `
        -DefaultRoute 'private'
    $ownership = Get-CloudConsoleOwnedTunnel -Adapter $Adapter -Routes $Routes
    switch ($ownership.State) {
        'None' {
            if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $true -and $context.Matches) {
                return New-CloudResult 4 '检查' 'check.unhealthy' "状态文件记录的隧道进程在（pid $($context.Pid)），但 18096 无监听。"
            }
            return New-CloudResult 1 '检查' 'check.absent' '18096 无监听，未发现自建隧道。'
        }
        'Foreign' {
            return New-CloudResult 3 '检查' 'check.foreign' "18096 被其他程序占用（$($ownership.Detail)）；未结束该进程。"
        }
        'Unconfirmed' {
            return New-CloudResult 3 '检查' 'check.unconfirmed' "18096 监听归属无法确认（$($ownership.Detail)）；未结束该进程。"
        }
        'Owned' {
            $health = Invoke-CloudConsoleHealthCheck -Adapter $Adapter -Uri $script:CloudConsoleHealthUrl `
                -TimeoutSeconds 3
            if ($health.Ok) {
                return New-CloudResult 0 '检查' 'check.healthy' "自建$($ownership.Route.Name)隧道健康（pid $($ownership.Pid)）。"
            }
            return New-CloudResult 4 '检查' 'check.unhealthy' "自建$($ownership.Route.Name)隧道进程在（pid $($ownership.Pid)），但健康检查失败：$($health.Detail)"
        }
    }
    return New-CloudResult 1 '检查' 'check.failed' '检查未完成。'
}

function Invoke-CloudConsoleStop {
    param($Adapter, $Routes, [string]$StatePath, [string]$LogPath, [int]$StopTimeoutSeconds)
    $state = Read-CloudConsoleState -StatePath $StatePath
    if ($null -eq $state -and (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        [void](Remove-CloudConsoleState -StatePath $StatePath)
    }
    $context = Resolve-CloudConsoleStateContext -Adapter $Adapter -State $state -Routes $Routes `
        -DefaultRoute 'private'
    $targetPid = 0
    $targetRoute = $null
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $null) {
        return New-CloudResult 1 '停止' 'stop.unconfirmed' "状态文件记录的进程无法确认（$($context.Error)）；未结束。"
    }
    if ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $true) {
        if ($context.Matches) {
            $targetPid = [int]$context.Pid
            $targetRoute = Get-CloudConsoleRoute -Routes $Routes -Name $context.Route
        } else {
            [void](Remove-CloudConsoleState -StatePath $StatePath)
            Write-CloudConsoleLog -Level 'WARN' -Event 'stop.stale' -Message '状态文件指向的进程不是本入口隧道，已清理过期状态。' -LogPath $LogPath
        }
    } elseif ($null -ne $context -and $null -ne $context.State -and $context.Alive -eq $false) {
        [void](Remove-CloudConsoleState -StatePath $StatePath)
    }
    if ($targetPid -le 0) {
        $ownership = Get-CloudConsoleOwnedTunnel -Adapter $Adapter -Routes $Routes
        if ($ownership.State -eq 'Owned') {
            $targetPid = [int]$ownership.Pid
            $targetRoute = $ownership.Route
        } elseif ($ownership.State -eq 'Foreign' -or $ownership.State -eq 'Unconfirmed') {
            return New-CloudResult 3 '停止' 'stop.foreign' "18096 被其他程序占用（$($ownership.Detail)）；未结束该进程。"
        }
    }
    if ($targetPid -le 0) {
        return New-CloudResult 0 '停止' 'stop.absent' '没有自建隧道需要停止。'
    }
    if ($null -eq $targetRoute) {
        return New-CloudResult 1 '停止' 'stop.failed' "无法确认待停止隧道（pid $targetPid）的路由；未结束该进程。"
    }
    # Re-read the listener after selecting the target PID: the state file may
    # point at one of our ssh processes while 18096 is actually owned by another
    # matching process.  Killing the recorded PID would then report success while
    # the tunnel stays up.
    $ownership = Get-CloudTunnelOwnership -Adapter $Adapter -ConfigPath $targetRoute.ConfigPath `
        -HostAlias $targetRoute.HostAlias -Route $targetRoute.Name
    if ($ownership.State -eq 'Foreign' -or $ownership.State -eq 'Unconfirmed') {
        return New-CloudResult 3 '停止' 'stop.foreign' "18096 被其他程序占用（$($ownership.Detail)）；未结束该进程。"
    }
    if ($ownership.State -eq 'Owned' -and [int]$ownership.Pid -ne $targetPid) {
        return New-CloudResult 3 '停止' 'stop.foreign' "18096 监听属于另一个匹配进程（pid $($ownership.Pid)），不是待停止的 pid $targetPid；未结束该进程。"
    }
    $stopped = $false
    try { $stopped = [bool](& $Adapter.StopProcess $targetPid) }
    catch { $stopped = $false }
    if (-not $stopped) {
        # A process that exited between the ownership check and Stop-Process is
        # already stopped; only report a failure when it is still alive or its
        # state cannot be read.
        $process = $null
        $processUnreadable = $false
        try { $process = & $Adapter.GetProcess $targetPid }
        catch { $processUnreadable = $true }
        if ($processUnreadable -or
                ($null -ne $process -and [bool](Get-CloudObjectProperty -Object $process -Name 'Alive'))) {
            return New-CloudResult 1 '停止' 'stop.failed' "无法停止自建隧道（pid $targetPid）；请手动检查该进程。"
        }
    }
    $attempts = [Math]::Max(1, [int][Math]::Ceiling(($StopTimeoutSeconds * 1000.0) / 100))
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $process = $null
        try { $process = & $Adapter.GetProcess $targetPid }
        catch { $process = $null }
        if ($null -eq $process -or -not [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) { break }
        if ($attempt -lt ($attempts - 1)) { & $Adapter.Sleep 100 }
    }
    $process = $null
    try { $process = & $Adapter.GetProcess $targetPid }
    catch { $process = $null }
    if ($null -ne $process -and [bool](Get-CloudObjectProperty -Object $process -Name 'Alive')) {
        return New-CloudResult 1 '停止' 'stop.failed' "自建隧道进程（pid $targetPid）仍在运行；请手动检查。"
    }
    if (-not (Remove-CloudConsoleState -StatePath $StatePath)) {
        return New-CloudResult 1 '停止' 'stop.statefailed' '隧道已停止，但状态文件清理失败。'
    }
    return New-CloudResult 0 '停止' 'stop.stopped' "已停止自建隧道（pid $targetPid）。"
}

function Invoke-CloudConsoleInstallShortcut {
    param($Adapter, [string]$DesktopPath, [string]$ShortcutName)
    $batExists = $false
    try { $batExists = [bool](& $Adapter.TestPath $script:CloudConsoleBatPath) }
    catch { return New-CloudResult 1 '快捷方式' 'shortcut.failed' "无法检查入口批处理：$($_.Exception.Message)" }
    if (-not $batExists) {
        return New-CloudResult 1 '快捷方式' 'shortcut.failed' "入口批处理不存在：$($script:CloudConsoleBatPath)"
    }
    if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
        try { $DesktopPath = [string](& $Adapter.GetDesktopPath) }
        catch { return New-CloudResult 1 '快捷方式' 'shortcut.failed' "无法定位桌面目录：$($_.Exception.Message)" }
    }
    if ([string]::IsNullOrWhiteSpace($DesktopPath) -or
            -not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
        return New-CloudResult 1 '快捷方式' 'shortcut.failed' "桌面目录不存在：$DesktopPath"
    }
    if ([string]::IsNullOrWhiteSpace($ShortcutName) -or $ShortcutName -match '[\\/:*?"<>|]') {
        return New-CloudResult 1 '快捷方式' 'shortcut.failed' '快捷方式名称非法。'
    }
    $shortcutPath = Join-Path $DesktopPath ($ShortcutName + '.lnk')
    try {
        $shell = & $Adapter.NewShortcutShell
        if ($null -eq $shell) { throw '无法创建 WScript.Shell' }
        $shortcut = $shell.CreateShortcut($shortcutPath)
        if ($null -eq $shortcut) { throw '无法创建快捷方式对象' }
        $shortcut.TargetPath = $script:CloudConsoleBatPath
        $shortcut.IconLocation = (Join-Path $script:CloudConsoleRoot 'assets\UM云端控制台-transparent.ico') + ',0'
        $shortcut.Arguments = ''
        $shortcut.WorkingDirectory = $script:CloudConsoleRoot
        $shortcut.Description = '通过 SSH 隧道打开 UM 云端控制台（私网优先，公网回退）'
        $shortcut.Save()
        return New-CloudResult 0 '快捷方式' 'shortcut.installed' "已安装桌面快捷方式：$shortcutPath"
    } catch {
        return New-CloudResult 1 '快捷方式' 'shortcut.failed' "安装桌面快捷方式失败：$($_.Exception.Message)"
    }
}

function Invoke-CloudConsoleEntry {
    param(
        [ValidateSet('Open', 'Stop', 'Check', 'InstallShortcut')][string]$Action = 'Open',
        [string]$SshConfig,
        [string]$SshHostAlias,
        [string]$PublicSshConfig,
        [string]$PublicSshHostAlias,
        [string]$StatePath,
        [string]$LogPath,
        [string]$DesktopPath = '',
        [string]$ShortcutName,
        [int]$HealthTimeoutSeconds = 25,
        [int]$HealthPollMilliseconds = 500,
        [int]$StopTimeoutSeconds = 10,
        [int]$LockTimeoutMilliseconds = 15000,
        [hashtable]$Adapter
    )
    $resolvedAdapter = Get-CloudConsoleAdapter -Adapter $Adapter
    if ([string]::IsNullOrWhiteSpace($SshConfig)) { $SshConfig = $script:CloudConsoleConfigPath }
    if ([string]::IsNullOrWhiteSpace($SshHostAlias)) { $SshHostAlias = $script:CloudConsoleHostAlias }
    if ([string]::IsNullOrWhiteSpace($PublicSshConfig)) { $PublicSshConfig = $script:CloudConsolePublicConfigPath }
    if ([string]::IsNullOrWhiteSpace($PublicSshHostAlias)) { $PublicSshHostAlias = $script:CloudConsolePublicHostAlias }
    if ([string]::IsNullOrWhiteSpace($StatePath)) { $StatePath = $script:CloudConsoleStatePath }
    if ([string]::IsNullOrWhiteSpace($LogPath)) { $LogPath = $script:CloudConsoleLogPath }
    if ([string]::IsNullOrWhiteSpace($ShortcutName)) { $ShortcutName = $script:CloudConsoleShortcutName }
    $routes = Get-CloudConsoleRoutes -SshConfig $SshConfig -SshHostAlias $SshHostAlias `
        -PublicSshConfig $PublicSshConfig -PublicSshHostAlias $PublicSshHostAlias

    $result = $null
    switch ($Action) {
        'Open' {
            $lock = $null
            try { $lock = & $resolvedAdapter.EnterLock $script:CloudConsoleLockName $LockTimeoutMilliseconds }
            catch { $lock = $null }
            if ($null -eq $lock) {
                $ownership = Get-CloudConsoleOwnedTunnel -Adapter $resolvedAdapter -Routes $routes
                if ($ownership.State -eq 'Owned') {
                    $health = Invoke-CloudConsoleHealthCheck -Adapter $resolvedAdapter `
                        -Uri $script:CloudConsoleHealthUrl -TimeoutSeconds ([Math]::Min(5, [Math]::Max(1, $HealthTimeoutSeconds)))
                    if ($health.Ok) {
                        $preflight = Get-CloudConsoleSshPreflight -Adapter $resolvedAdapter `
                            -SshConfig $ownership.Route.ConfigPath -SshHostAlias $ownership.Route.HostAlias `
                            -RequireTailscaleAddress ([bool]$ownership.Route.RequireTailscaleAddress)
                        if ($preflight.ExitCode -ne 0) {
                            $result = $preflight
                        } else {
                            $result = Open-CloudConsoleAuthenticatedPage -Adapter $resolvedAdapter -SshPreflight $preflight `
                                -ConfigPath $ownership.Route.ConfigPath -HostAlias $ownership.Route.HostAlias
                        }
                    } else {
                        $result = New-CloudResult 4 '健康' 'open.unhealthy' "自建$($ownership.Route.Name)隧道进程在（pid $($ownership.Pid)），但健康检查失败：$($health.Detail)"
                    }
                } elseif ($ownership.State -eq 'Foreign') {
                    $result = New-CloudResult 3 '端口' 'open.foreign' "18096 被其他程序占用（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。"
                } elseif ($ownership.State -eq 'Unconfirmed') {
                    $result = New-CloudResult 3 '端口' 'open.unconfirmed' "18096 监听归属无法确认（$($ownership.Detail)）；拒绝冒用，也不会结束该进程。"
                } else {
                    $result = New-CloudResult 1 '错误' 'open.locked' '另一个入口正在启动隧道，请稍候再试。'
                }
            } else {
                try {
                    $result = Invoke-CloudConsoleOpen -Adapter $resolvedAdapter -Routes $routes `
                        -StatePath $StatePath -LogPath $LogPath `
                        -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds
                } finally {
                    & $resolvedAdapter.ExitLock $lock
                }
            }
        }
        'Stop' {
            $lock = $null
            try { $lock = & $resolvedAdapter.EnterLock $script:CloudConsoleLockName $LockTimeoutMilliseconds }
            catch { $lock = $null }
            if ($null -eq $lock) {
                $result = New-CloudResult 1 '停止' 'stop.locked' '另一个入口正在操作隧道，请稍候再试。'
            } else {
                try {
                    $result = Invoke-CloudConsoleStop -Adapter $resolvedAdapter -Routes $routes `
                        -StatePath $StatePath -LogPath $LogPath `
                        -StopTimeoutSeconds $StopTimeoutSeconds
                } finally {
                    & $resolvedAdapter.ExitLock $lock
                }
            }
        }
        'Check' {
            $result = Invoke-CloudConsoleCheck -Adapter $resolvedAdapter -Routes $routes `
                -StatePath $StatePath -LogPath $LogPath
        }
        'InstallShortcut' {
            $result = Invoke-CloudConsoleInstallShortcut -Adapter $resolvedAdapter `
                -DesktopPath $DesktopPath -ShortcutName $ShortcutName
        }
    }
    if ($null -eq $result) {
        $result = New-CloudResult 1 '错误' 'entry.failed' '入口未返回结果。'
    }
    $level = if ($result.ExitCode -eq 0) { 'INFO' } else { 'WARN' }
    Write-CloudConsoleLog -Level $level -Event $result.Event -Message $result.Message -LogPath $LogPath
    Write-Host ('[{0}] {1}' -f $result.Label, $result.Message)
    return [int]$result.ExitCode
}

if (-not $Library -and $MyInvocation.InvocationName -ne '.') {
    $selected = @()
    if ($Stop) { $selected += 'Stop' }
    if ($Check) { $selected += 'Check' }
    if ($InstallShortcut) { $selected += 'InstallShortcut' }
    if ($selected.Count -gt 1) {
        [Console]::Error.WriteLine('ERROR: -Stop、-Check、-InstallShortcut 只能选择一个。')
        exit 2
    }
    $action = if ($selected.Count -eq 1) { $selected[0] } else { 'Open' }
    exit (Invoke-CloudConsoleEntry -Action $action -SshConfig $SshConfig `
        -SshHostAlias $SshHostAlias -PublicSshConfig $PublicSshConfig `
        -PublicSshHostAlias $PublicSshHostAlias -StatePath $StatePath -LogPath $LogPath `
        -DesktopPath $DesktopPath -ShortcutName $ShortcutName `
        -HealthTimeoutSeconds $HealthTimeoutSeconds -HealthPollMilliseconds $HealthPollMilliseconds `
        -StopTimeoutSeconds $StopTimeoutSeconds -LockTimeoutMilliseconds $LockTimeoutMilliseconds)
}
