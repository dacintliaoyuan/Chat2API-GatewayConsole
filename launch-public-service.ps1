param([Parameter(Mandatory=$true)][ValidateSet('chat2api','usage')][string]$Key)
Set-StrictMode -Version 3.0
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
. (Join-Path $root 'lib\public-launcher.ps1')
try {
    Reset-PublicInheritedPaths
    # Preserve direct gateway routing without changing the user's system proxy.
    foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
        [Environment]::SetEnvironmentVariable($name,$null,'Process')
    }
    $env:NO_PROXY='*'
    $env:no_proxy='*'
    $python=Resolve-PublicServicePython $root
    Assert-PublicPythonVersion $python
    if ($Key -eq 'chat2api') {
        $upstream=[string]$env:AI_GATEWAY_OPENWEBUI_URL
        if (-not $upstream) { $upstream=Read-PublicLauncherSettings $root }
        if (-not $upstream) { $upstream='http://localhost:3000' }
        $upstream=Resolve-PublicUpstreamUrl $upstream
        & $python (Join-Path $root 'chat2api\chat2api.py') --base-url $upstream
    } else { & $python (Join-Path $root 'usagesrv.py') }
    exit $LASTEXITCODE
} catch { Write-Error $_; exit 1 }
