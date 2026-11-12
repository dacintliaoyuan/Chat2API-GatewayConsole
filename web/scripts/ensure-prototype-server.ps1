# Ensure the AI gateway console prototype dev server is listening on 127.0.0.1:5173.
# Idempotent: if the port answers, do nothing; otherwise start `npm run dev`
# detached (hidden window) and wait until the port accepts connections.
# Exit codes: 0 = server reachable, 1 = could not start/verify.

$ErrorActionPreference = "Stop"

# scripts/ sits directly under the console root.
$prototypeRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent (Split-Path -Parent $prototypeRoot)
$logFile = Join-Path $repoRoot "tmp\console-dev.log"

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait(500)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (Test-Port 5173) {
    Write-Output "[AI网关控制台-原型] 服务已在 127.0.0.1:5173 运行，直接打开。"
    exit 0
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $npm) {
    Write-Output "[AI网关控制台-原型] 未找到 npm，无法启动原型服务。"
    exit 1
}

if (-not (Test-Path (Join-Path $prototypeRoot "package.json"))) {
    Write-Output "[AI网关控制台-原型] 未找到原型目录（$prototypeRoot），请检查文件位置。"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null
# No quotes around the command itself (no spaces in "npm run dev"): with /s cmd
# runs it verbatim and only the log path stays quoted for the redirect.
$arguments = "/d /s /c npm run dev >> `"$logFile`" 2>&1"
Start-Process -FilePath "cmd.exe" -ArgumentList $arguments -WorkingDirectory $prototypeRoot -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port 5173) {
        Write-Output "[AI网关控制台-原型] 服务已启动（127.0.0.1:5173）。"
        exit 0
    }
}

Write-Output "[AI网关控制台-原型] 等待超时：服务未在 30 秒内就绪，日志见 $logFile 。"
exit 1
