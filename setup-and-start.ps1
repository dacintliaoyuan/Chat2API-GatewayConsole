[CmdletBinding()]
param([string]$UpstreamUrl='', [switch]$SetupOnly, [switch]$CheckOnly, [switch]$NoBrowser)

Set-StrictMode -Version 3.0
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
. (Join-Path $root 'lib\public-launcher.ps1')
. (Join-Path $root 'lib\gateway-runtime.ps1')
. (Join-Path $root 'lib\console-runtime-bridge.ps1')
$mutex=$null
$held=$false

try {
    $python=Find-PublicPython
    $node=Find-PublicNode
    if ($CheckOnly) {
        $states=@('chat2api','usage' | ForEach-Object { Get-PublicStartInspection -Key $_ })
        [pscustomobject]@{Python=$python.Version;Node=$node.Version;Services=$states;ReadOnly=$true} | ConvertTo-Json -Depth 4
        exit 0
    }
    $mutexHash=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant())))).Replace('-','')
    $mutex=New-Object Threading.Mutex($false,('Local\AIGatewayPublicSetup-'+$mutexHash))
    try { $held=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held=$true }
    if (-not $held) { throw '本目录的一键部署已经在执行，请等待那个窗口结束。' }
    if (-not $SetupOnly) {
        foreach ($key in @('chat2api','usage')) { Assert-PublicStartAllowed (Get-PublicStartInspection $key) }
    }

    if (-not $UpstreamUrl) { $UpstreamUrl=[string]$env:AI_GATEWAY_OPENWEBUI_URL }
    if (-not $UpstreamUrl) { $UpstreamUrl=Read-PublicLauncherSettings $root }
    if (-not $UpstreamUrl) {
        $UpstreamUrl=Read-Host '首次部署：输入 Open WebUI 上游地址（直接回车使用 http://localhost:3000）'
        if (-not $UpstreamUrl) { $UpstreamUrl='http://localhost:3000' }
    }
    $UpstreamUrl=Resolve-PublicUpstreamUrl $UpstreamUrl
    $front=Join-Path $root 'web'
    $venvPython=Join-Path $root '.venv\Scripts\python.exe'
    $stateFile=Join-Path $root 'data\setup-state.json'
    $state=$null
    if (Test-Path -LiteralPath $stateFile) {
        $state=Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $requirementsHash=(Get-FileHash -LiteralPath (Join-Path $root 'requirements.txt') -Algorithm SHA256).Hash
    $frontHash=Get-PublicInputsHash $front @('src','public','scripts','index.html','package.json','package-lock.json','tsconfig.json','vite.config.ts')
    Write-Host '[1/4] 准备项目内 Python 环境…'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-PublicCommand $python.Executable (@($python.Prefix)+@('-m','venv',(Join-Path $root '.venv'))) $root
    }
    Assert-PublicPythonVersion $venvPython
    & $venvPython -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ('requests','playwright')) else 1)"
    $pythonReady=$LASTEXITCODE -eq 0
    if (-not $pythonReady -or $null -eq $state -or -not $state.PSObject.Properties['requirements_hash'] -or $state.requirements_hash -ne $requirementsHash) {
        Invoke-PublicCommand $venvPython @('-m','pip','install','-r',(Join-Path $root 'requirements.txt')) $root
    }
    Write-Host '[2/4] 安装前端依赖并构建页面…'
    $index=Join-Path $front 'dist\index.html'
    if ($null -eq $state -or -not $state.PSObject.Properties['frontend_hash'] -or $state.frontend_hash -ne $frontHash -or -not (Test-Path -LiteralPath $index)) {
        Invoke-PublicCommand $node.Npm @('ci','--ignore-scripts') $front
        Invoke-PublicCommand $node.Npm @('run','check:scope') $front
        Invoke-PublicCommand $node.Npm @('run','build') $front
    }
    Write-Host '[3/4] 保存本机启动配置…'
    Write-PublicLauncherSettings $root $UpstreamUrl
    $env:AI_GATEWAY_OPENWEBUI_URL=$UpstreamUrl
    $temporary=$stateFile+'.'+$PID+'.tmp'
    try {
        [IO.File]::WriteAllText($temporary,(@{version=1;requirements_hash=$requirementsHash;frontend_hash=$frontHash} | ConvertTo-Json),(New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $stateFile -Force
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
    if ($SetupOnly) { Write-Host '部署准备完成。未启动服务、浏览器、分享或计划任务。'; exit 0 }

    Write-Host '[4/4] 启动网关与控制台…'
    foreach ($key in @('chat2api','usage')) {
        $lock=Enter-ConsoleResourceLock -Resource ('service:'+$key) -Owner 'public-setup'
        try {
            $inspection=Get-PublicStartInspection $key
            Assert-PublicStartAllowed $inspection
            if ($inspection.State -eq 'Running') { Write-Host ($key+' 已运行，保持当前实例。'); continue }
            $result=Start-PublicService -Key $key
            if ($result.Status -notin @('Started','AlreadyRunning')) { throw ($key+' 未确认启动成功（'+$result.Status+'）；请查看本目录日志，不会重启其他实例。') }
        } finally { Exit-ConsoleResourceLock -Lock $lock }
    }
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8096/' }
    Write-Host '已就绪：http://127.0.0.1:8096/；API：http://127.0.0.1:8000/v1'
    Write-Host '首次使用请在账号页登录上游。分享、系统账户和守护计划任务需要另行配置。'
    exit 0
} catch {
    Write-Host ('失败：'+$_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($null -ne $mutex) { if ($held) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
}
