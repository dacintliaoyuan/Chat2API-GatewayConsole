# Ensure the AI gateway console CANDIDATE preview is listening on 127.0.0.1:8099.
# Idempotent: if the port answers, do nothing; otherwise start `vite preview`
# detached (hidden window) and wait until the port accepts connections.
#
# The candidate is served from release/candidate (a gitignored build output) and its
# proxy table is READ-ONLY: vite.config.ts gives `preview` its own proxy that never
# rewrites the Origin header, plus a middleware (candidateReadOnlyGuard) that answers
# every non-GET/HEAD request, on any path, with a local 403 before the proxy - no
# exception for actions the backend declares read-only. So clicking a management
# button here cannot change production state or add a production ledger row; use the
# 8001 production page for real operations. A config change reaches a RUNNING
# preview only after it is restarted: this script starts one only when 8099 is idle.
#
# Exit codes: 0 = candidate preview reachable, 1 = could not start/verify.

$ErrorActionPreference = "Stop"

# scripts/ sits directly under the console root.
$prototypeRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent (Split-Path -Parent $prototypeRoot)
$logFile = Join-Path $repoRoot "tmp\console-candidate-preview.log"
$candidateDir = Join-Path $prototypeRoot "release\candidate"

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

if (Test-Port 8099) {
    Write-Output "[AI gateway console - candidate] already listening on 127.0.0.1:8099."
    exit 0
}

if (-not (Test-Path (Join-Path $candidateDir "index.html"))) {
    Write-Output "[AI gateway console - candidate] no build at release/candidate/index.html."
    Write-Output "Build it first: npx tsc && npx vite build --mode candidate"
    exit 1
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $npm) {
    Write-Output "[AI gateway console - candidate] npm not found; cannot start the preview."
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null
# No quotes around the command itself (no spaces in it): with /s cmd runs it
# verbatim and only the log path stays quoted for the redirect.
$arguments = "/d /s /c npx vite preview --outDir release/candidate --port 8099 --strictPort >> `"$logFile`" 2>&1"
Start-Process -FilePath "cmd.exe" -ArgumentList $arguments -WorkingDirectory $prototypeRoot -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port 8099) {
        Write-Output "[AI gateway console - candidate] listening on 127.0.0.1:8099 (read-only preview)."
        exit 0
    }
}

Write-Output "[AI gateway console - candidate] timed out after 30s; see $logFile"
exit 1
