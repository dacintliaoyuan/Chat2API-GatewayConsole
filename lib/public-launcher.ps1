# Functions only: no install, file creation, service start or browser at import.
Set-StrictMode -Version 3.0

function Resolve-PublicUpstreamUrl {
    param([Parameter(Mandatory=$true)][string]$Value)
    $url=$null
    if (-not [uri]::TryCreate($Value.Trim(),[UriKind]::Absolute,[ref]$url) -or
        $url.Scheme -notin @('http','https') -or -not $url.Host -or
        $url.UserInfo -or $url.Query -or $url.Fragment -or $Value -match '[\r\n]') {
        throw '上游地址必须是 http/https 基础地址，不能含用户名、密码、查询参数或片段。'
    }
    return $url.AbsoluteUri.TrimEnd('/')
}

function Read-PublicLauncherSettings {
    param([Parameter(Mandatory=$true)][string]$Root)
    $file=Join-Path $Root 'data\launcher-settings.json'
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $settings=Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $settings -or -not $settings.PSObject.Properties['version'] -or
        $settings.version -ne 1 -or -not $settings.PSObject.Properties['upstream_url']) {
        throw '启动配置损坏或版本不兼容；请核对 data/launcher-settings.json。'
    }
    return (Resolve-PublicUpstreamUrl ([string]$settings.upstream_url))
}

function Write-PublicLauncherSettings {
    param([Parameter(Mandatory=$true)][string]$Root,
          [Parameter(Mandatory=$true)][string]$UpstreamUrl)
    $url=Resolve-PublicUpstreamUrl $UpstreamUrl
    $directory=Join-Path $Root 'data'
    [void][IO.Directory]::CreateDirectory($directory)
    $file=Join-Path $directory 'launcher-settings.json'
    $temporary=Join-Path $directory ('launcher-settings.{0}.tmp' -f [guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($temporary,(@{version=1;upstream_url=$url} | ConvertTo-Json),
                              (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $file -Force -ErrorAction Stop
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Invoke-PublicCommand {
    param([Parameter(Mandatory=$true)][string]$Executable,
          [string[]]$Arguments=@(),
          [Parameter(Mandatory=$true)][string]$WorkingDirectory)
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Executable @Arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw ('命令执行失败（退出码 {0}）：{1}' -f $LASTEXITCODE,[IO.Path]::GetFileName($Executable)) }
    } finally { Pop-Location }
}

function Find-PublicPython {
    foreach ($name in @('python.exe','py.exe')) {
        $command=Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
        if ($null -eq $command) { continue }
        $prefix=@()
        if ($name -eq 'py.exe') { $prefix=@('-3') }
        $probe=& $command.Source @prefix --version 2>$null
        if ($LASTEXITCODE -eq 0 -and "$probe" -match '^Python (\d+\.\d+\.\d+)$') {
            $version=$Matches[1]
            if ([version]$version -ge [version]'3.11') {
                return [pscustomobject]@{Executable=$command.Source;Prefix=$prefix;Version=$version}
            }
        }
    }
    throw '未找到 Python 3.11+。请先安装 Python（python.org），勾选 PATH，然后再次双击启动器。'
}

function Find-PublicNode {
    $node=Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $npm=Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $node -or $null -eq $npm) { throw '未找到 Node.js/npm。请先安装 Node.js 22.12+（nodejs.org），然后再次双击启动器。' }
    $probe=& $node.Source --version
    if ($LASTEXITCODE -ne 0 -or "$probe" -notmatch '^v\d+\.\d+\.\d+$' -or [version]"$probe".TrimStart('v') -lt [version]'22.12') {
        throw 'Node.js 需要 22.12 或更新版本。'
    }
    return [pscustomobject]@{Executable=$node.Source;Npm=$npm.Source;Version=[string]$probe}
}

function Assert-PublicPythonVersion {
    param([Parameter(Mandatory=$true)][string]$Executable,
          [scriptblock]$Reader={ param($path)
              $output=& $path --version 2>$null
              [pscustomobject]@{Output=[string]$output;ExitCode=$LASTEXITCODE}
          })
    $probe=& $Reader $Executable
    if ($probe.ExitCode -ne 0 -or $probe.Output -notmatch '^Python (\d+\.\d+\.\d+)$' -or [version]$Matches[1] -lt [version]'3.11') {
        throw '本目录的 Python 环境不可用或低于 3.11；请自行检查 .venv 后重新部署，启动器不会删除已有环境。'
    }
}

function Get-PublicInputsHash {
    param([Parameter(Mandatory=$true)][string]$Root,[Parameter(Mandatory=$true)][string[]]$Paths)
    $rows=New-Object 'System.Collections.Generic.List[string]'
    foreach ($relative in $Paths) {
        $path=Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $path)) { throw ('缺少部署输入：{0}' -f $relative) }
        $files=@(Get-Item -LiteralPath $path)
        if ($files[0].PSIsContainer) { $files=@(Get-ChildItem -LiteralPath $path -File -Recurse) }
        foreach ($file in $files) {
            $name=$file.FullName.Substring($Root.TrimEnd('\').Length).TrimStart('\').Replace('\','/')
            $rows.Add($name+' '+(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)
        }
    }
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes((@($rows | Sort-Object) -join "`n"))))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-PublicStartAllowed {
    param([Parameter(Mandatory=$true)]$Inspection)
    if ($Inspection.State -notin @('Stopped','Running')) {
        throw ('{0} 无法直接启动（{1}）。外来端口占用、残留或健康异常均不自动重启；请先在原控制台明确处理。' -f $Inspection.Key,$Inspection.State)
    }
}

function Get-PublicStartInspection {
    param([Parameter(Mandatory=$true)][string]$Key,
          [scriptblock]$ListenerReader={ @(Get-NetTCPConnection -State Listen -ErrorAction Stop) },
          [scriptblock]$ProcessReader={ @(Get-CimInstance Win32_Process -ErrorAction Stop) },
          [scriptblock]$HealthChecker={ param($k) Test-PublicServiceHealth -Key $k })
    # Query failures are not proof that a port or process is absent.
    $descriptor=$script:GatewayServiceDescriptors[$Key]
    if ($null -eq $descriptor) { throw '未知服务。' }
    $listeners=@(& $ListenerReader | Where-Object { $_.LocalPort -eq $descriptor.Port })
    $processes=@(& $ProcessReader)
    foreach ($listener in $listeners) {
        $owners=@($processes | Where-Object { $_.ProcessId -eq $listener.OwningProcess })
        if ($owners.Count -ne 1 -or -not (Test-GatewayProcessOwnership -Descriptor $descriptor -Process $owners[0])) {
            return [pscustomobject]@{Key=$Key;State='Conflict'}
        }
    }
    if ($listeners.Count -gt 0) {
        $state='Degraded'
        if (& $HealthChecker $Key) { $state='Running' }
        return [pscustomobject]@{Key=$Key;State=$state}
    }
    $owned=@($processes | Where-Object { Test-GatewayProcessOwnership -Descriptor $descriptor -Process $_ })
    return [pscustomobject]@{Key=$Key;State=$(if ($owned.Count) {'Residual'} else {'Stopped'})}
}

function Resolve-PublicServicePython {
    param([Parameter(Mandatory=$true)][string]$Root)
    $venv=Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv -PathType Leaf) { return $venv }
    $python=Find-PublicPython
    if ($python.Prefix.Count -gt 0) { throw '请先运行一键部署启动器创建 .venv，再启动服务。' }
    return $python.Executable
}

function Test-PublicServiceHealth {
    param([Parameter(Mandatory=$true)][string]$Key,
          [scriptblock]$Request={ param($url) Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop })
    $descriptor=$script:GatewayServiceDescriptors[$Key]
    if ($null -eq $descriptor) { throw 'Unknown service.' }
    try {
        $response=& $Request ('http://127.0.0.1:'+ $descriptor.Port + $descriptor.HealthPath)
        # HTTP error responses prove a listener, not a healthy service.
        return ($null -ne $response -and [int]$response.StatusCode -eq 200)
    } catch { return $false }
}

function Start-PublicService {
    param([Parameter(Mandatory=$true)][string]$Key,
          [scriptblock]$Inspector={ param($k) Get-PublicStartInspection -Key $k },
          [scriptblock]$Launcher={ param($path)
              $saved=[Environment]::GetEnvironmentVariable('AIGATEWAY_PUBLIC_HIDDEN_LAUNCH','Process')
              try {
                  $env:AIGATEWAY_PUBLIC_HIDDEN_LAUNCH='1'
                  Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d','/c',"`"$path`"") -WorkingDirectory (Split-Path -Parent $path) -WindowStyle Hidden
              } finally { [Environment]::SetEnvironmentVariable('AIGATEWAY_PUBLIC_HIDDEN_LAUNCH',$saved,'Process') }
          },
          [scriptblock]$Sleeper={ Start-Sleep -Seconds 1 })
    # Recheck at the launch boundary. Never delegate to the runtime's cleanup
    # start path, which intentionally stops degraded/residual instances.
    $inspection=& $Inspector $Key
    Assert-PublicStartAllowed $inspection
    if ($inspection.State -eq 'Running') { return [pscustomobject]@{Status='AlreadyRunning'} }
    $path=Resolve-GatewayStartPath -Key $Key
    & $Launcher $path | Out-Null
    $timeout=$script:GatewayServiceDescriptors[$Key].StartTimeoutSeconds
    for ($attempt=0; $attempt -lt $timeout; $attempt++) {
        & $Sleeper | Out-Null
        $inspection=& $Inspector $Key
        if ($inspection.State -eq 'Running') { return [pscustomobject]@{Status='Started'} }
        if ($inspection.State -eq 'Conflict') { throw 'Port ownership changed during startup; no process was stopped.' }
    }
    return [pscustomobject]@{Status='StartPending'}
}

function Reset-PublicInheritedPaths {
    # Local state must belong to this copy. Keep the explicitly documented
    # upstream URL and optional downstream API key; clear only path overrides
    # in this child process, never the user's system environment.
    foreach ($name in @('CHAT2API_TOKEN_FILE','CHAT2API_SERVICE_LOG','CHAT2API_USAGE_LOG',
        'CHAT2API_CONTROL_TOKEN_FILE','CHAT2API_REAUTH_SLOT_FILE','CHAT2API_PRICE_TABLE_FILE',
        'CHAT2API_SHARE_ATTRIBUTION_KEY_FILE','SHARE_ATTRIBUTION_KEY_FILE',
        'USAGE_PRICE_FILE','USAGE_SESSION_CODE_FILE','USAGE_CONTROL_TOKEN_FILE',
        'CONSOLE_CONTROL_TOKEN_FILE','CONSOLE_STATE_FILE','CONSOLE_LOCK_DIR',
        'SHARE_MANAGEMENT_CONFIG','SHARE_STORE_FILE','CONSOLE_SHARE_BACKEND_DIR',
        'CONSOLE_SHARE_BACKEND_PORT','CONSOLE_SHARE_BACKEND_BASE_URL',
        'CONSOLE_SHARE_RUN_ROOT','CONSOLE_SHARE_ATTRIBUTION_KEY_FILE')) {
        [Environment]::SetEnvironmentVariable($name,$null,'Process')
    }
}
