# 一键部署本机网关（Rust 8000/8001）。
#
# 新克隆的机器上运行一次即可：检查工具链、构建 Rust release 二进制、构建
# web/dist、准备 data/ 与控制令牌、启动 8000 与 8001、创建/修复桌面快捷方式，
# 最后打开控制台。重复运行是幂等的：已运行的进程不重启，已有快捷方式不改
# 名字和图标（只修复指向已移动旧路径的目标）。
#
# 回滚点仍是 start_chat2api.bat / start_usage_service.bat：把
# lib\gateway-runtime.ps1 的 StartPath 改回批处理即可，不需要重装。
param(
    [switch]$SkipRustBuild,
    [switch]$SkipWebBuild,
    [switch]$SkipShortcut,
    [switch]$NoOpen
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$rustRoot = Join-Path $root 'rust'
$webRoot = Join-Path $root 'web'
$dataRoot = Join-Path $root 'data'
$localEntry = Join-Path $root 'scripts\Open-GatewayConsole.ps1'
$serviceCli = Join-Path $root 'invoke-gateway-service.ps1'

function Stop-Deploy {
    param([string]$Message)
    [Console]::Error.WriteLine("[部署] $Message")
    exit 1
}

function Test-Health {
    param([string]$Url, [int]$Attempts = 30, [int]$IntervalMilliseconds = 500)
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            # 尚未就绪，继续有界等待。
        }
        Start-Sleep -Milliseconds $IntervalMilliseconds
    }
    return $false
}

# 1) Rust 工具链与 release 二进制。
if (-not $SkipRustBuild) {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        Stop-Deploy '未找到 cargo。先安装 Rust 工具链（https://rustup.rs），或加 -SkipRustBuild 复用已有二进制。'
    }
    Push-Location $rustRoot
    try {
        & cargo build --release -p um-gateway -p um-console
        if ($LASTEXITCODE -ne 0) { Stop-Deploy 'Rust release 构建失败。' }
    } finally {
        Pop-Location
    }
}

# 2) 本机生产页 web/dist（local 角色）。
if (-not $SkipWebBuild) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Stop-Deploy '未找到 npm。先安装 Node.js，或加 -SkipWebBuild 复用已有 web/dist。'
    }
    Push-Location $webRoot
    try {
        if (-not (Test-Path (Join-Path $webRoot 'node_modules'))) {
            & npm ci
            if ($LASTEXITCODE -ne 0) { Stop-Deploy 'npm ci 失败。' }
        }
        $env:CONSOLE_PRODUCTION_BUILD = '1'
        $env:CONSOLE_ROLE = 'local'
        $env:CONSOLE_LOCAL_SHARE_RETIRED = '1'
        try {
            & npm run build
            if ($LASTEXITCODE -ne 0) { Stop-Deploy '前端构建失败。' }
        } finally {
            Remove-Item Env:CONSOLE_PRODUCTION_BUILD -ErrorAction SilentlyContinue
            Remove-Item Env:CONSOLE_ROLE -ErrorAction SilentlyContinue
            Remove-Item Env:CONSOLE_LOCAL_SHARE_RETIRED -ErrorAction SilentlyContinue
        }
    } finally {
        Pop-Location
    }
}

# 3) 运行数据目录与控制令牌。网关首次启动也会创建令牌；这里提前准备，
#    让 8001 的会话引导码在首次打开时就能签发。
New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
$tokenPath = Join-Path $dataRoot 'admin-control-token'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $token = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    [IO.File]::WriteAllText($tokenPath, $token, [Text.UTF8Encoding]::new($false))
}

# 4) 启动 8000，再启动 8001。已就绪的服务不重启：端口可能还被其他
#    程序监听（例如 tailscaled 或本地工具），此时只认本机 /health，不碰别人。
if (-not (Test-Health -Url 'http://127.0.0.1:8000/health' -Attempts 2)) {
    $global:LASTEXITCODE = 0
    & $serviceCli -Key chat2api -Action Start
    if ($LASTEXITCODE -ne 0) { Stop-Deploy 'chat2api（8000）启动失败。' }
}
if (-not (Test-Health -Url 'http://127.0.0.1:8000/health')) {
    Stop-Deploy 'chat2api（8000）未在等待上限内就绪。'
}

if (-not (Test-Health -Url 'http://127.0.0.1:8001/health' -Attempts 2)) {
    $global:LASTEXITCODE = 0
    & $serviceCli -Key usage -Action Start
    if ($LASTEXITCODE -ne 0) { Stop-Deploy '用量统计（8001）启动失败。' }
}
if (-not (Test-Health -Url 'http://127.0.0.1:8001/health')) {
    Stop-Deploy '用量统计（8001）未在等待上限内就绪。'
}

# 5) 桌面快捷方式：不存在时用云端图标创建；已存在时修复指向已移动旧路径的
#    目标，并把图标换成云端图标；名字不改。
if (-not $SkipShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ([string]::IsNullOrWhiteSpace($desktop) -or
            -not (Test-Path -LiteralPath $desktop -PathType Container)) {
        Stop-Deploy "桌面目录不存在：$desktop"
    }
    $shell = New-Object -ComObject WScript.Shell
    $localShortcutPath = Join-Path $desktop '本地控制台.lnk'
    $localTarget = Join-Path $root '一键部署本机网关.bat'
    $localIcon = Join-Path $root 'assets\UM云端控制台.ico'
    if (Test-Path -LiteralPath $localShortcutPath) {
        $shortcut = $shell.CreateShortcut($localShortcutPath)
        $oldTarget = Join-Path $root 'Open-GatewayConsole.bat'
        $changed = $false
        if ($shortcut.TargetPath -eq $oldTarget) {
            $shortcut.TargetPath = $localTarget
            $changed = $true
        }
        if ($shortcut.IconLocation -notlike "$localIcon,*") {
            $shortcut.IconLocation = $localIcon + ',0'
            $changed = $true
        }
        if ($changed) { $shortcut.Save() }
    } else {
        $shortcut = $shell.CreateShortcut($localShortcutPath)
        $shortcut.TargetPath = $localTarget
        $shortcut.IconLocation = $localIcon + ',0'
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = '一键部署并打开本机 UM 网关控制台'
        $shortcut.Save()
    }
    # 云端入口已收进 cloud/：只修复指向旧根路径的云端快捷方式，不新建、不改名字和图标。
    $cloudShortcutPath = Join-Path $desktop '云端控制台.lnk'
    if (Test-Path -LiteralPath $cloudShortcutPath) {
        $cloudShortcut = $shell.CreateShortcut($cloudShortcutPath)
        if ($cloudShortcut.TargetPath -eq (Join-Path $root 'Open-CloudConsole.bat')) {
            $cloudShortcut.TargetPath = Join-Path $root 'cloud\Open-CloudConsole.bat'
            if ($cloudShortcut.Arguments -match 'CloudConsolePort\s+18096') {
                $cloudShortcut.Arguments = $cloudShortcut.Arguments -replace `
                    'CloudConsolePort\s+18096', 'CloudConsolePort 8002'
            }
            $cloudShortcut.Save()
        }
    }
}

# 6) 打开控制台（会复用已启动的 8001，并换取一次性引导码）。
if (-not $NoOpen) {
    & $localEntry
    exit $LASTEXITCODE
}
Write-Host '[部署] 本机网关已就绪：http://127.0.0.1:8001/'
exit 0
