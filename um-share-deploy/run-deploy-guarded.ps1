# run-deploy-guarded.ps1 — elevates deploy-elevated.ps1 and runs it via -File
# with plain arguments (no -Command redirection, whose quoting is fragile).
# Observability comes from the deploy script's own readable log under LogDir.
param(
    [Parameter(Mandatory = $true)][ValidateSet('Prep', 'Canary', 'Start', 'Stop', 'DrainStop', 'PublishProjection', 'Watchdog')][string]$Stage,
    [ValidateSet('probe', 'real')][string]$Mode = 'probe',
    [string]$Deploy = 'D:\git\github\personal_files\SelfUseAI\ai-gateway\um-share-deploy\deploy-elevated.ps1'
)
$ErrorActionPreference = 'Continue'
# $Deploy has no spaces; do NOT wrap it in quotes - Start-Process ArgumentList
# joins elements with spaces and the extra quoting intermittently broke spawning.
$argsList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Deploy, '-Stage', $Stage, '-Mode', $Mode)
try {
    $powerShellHost = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
    if (-not (Test-Path -LiteralPath $powerShellHost -PathType Leaf)) { $powerShellHost = (Get-Command pwsh -ErrorAction Stop).Source }
    $p = Start-Process -FilePath $powerShellHost -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList $argsList
    Write-Output ("ELEVATED-EXIT-CODE: " + $p.ExitCode)
    exit $p.ExitCode
} catch {
    Write-Output ("ELEVATION-FAILED: " + $_.Exception.Message)
    exit 1
}
