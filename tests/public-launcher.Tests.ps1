# Pure launcher checks: temporary files and injected process/network facts only.
Set-StrictMode -Version 3.0
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'lib\public-launcher.ps1')
. (Join-Path $root 'lib\gateway-runtime.ps1')
$tempBase=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temp=Join-Path $tempBase ('AIGatewayPublicLauncherTests-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($temp)
$script:passed=0
function Check($name,[scriptblock]$body) { & $body; $script:passed++; Write-Host ('PASS '+$name) }
function Require($condition,$message='assertion failed') { if (-not $condition) { throw $message } }
function MustThrow([scriptblock]$body) { $threw=$false; try { & $body | Out-Null } catch { $threw=$true }; Require $threw 'expected refusal' }
try {
    Check 'normalize upstream' { Require ((Resolve-PublicUpstreamUrl 'https://example.test/webui/') -eq 'https://example.test/webui') }
    Check 'reject secret-bearing and invalid URLs' {
        foreach ($url in @('file:///tmp','not-a-url','https://user:pass@example.test','https://example.test/?token=fixture','https://example.test/#token',"https://example.test/`n")) {
            MustThrow { Resolve-PublicUpstreamUrl $url }
        }
    }
    Check 'settings write/read' {
        Require ($null -eq (Read-PublicLauncherSettings $temp))
        Write-PublicLauncherSettings $temp 'http://localhost:3000'
        Require ((Read-PublicLauncherSettings $temp) -eq 'http://localhost:3000')
        Write-PublicLauncherSettings $temp 'https://example.test'
        Require ((Read-PublicLauncherSettings $temp) -eq 'https://example.test')
        Require (@(Get-ChildItem (Join-Path $temp 'data') -Filter '*.tmp').Count -eq 0)
    }
    Check 'invalid settings never fall back silently' {
        $file=Join-Path $temp 'data\launcher-settings.json'
        [IO.File]::WriteAllText($file,'{"version":2,"upstream_url":"https://example.test"}')
        MustThrow { Read-PublicLauncherSettings $temp }
        [IO.File]::WriteAllText($file,'{broken')
        MustThrow { Read-PublicLauncherSettings $temp }
    }
    Check 'fingerprint reflects source changes' {
        $file=Join-Path $temp 'input.txt'
        [IO.File]::WriteAllText($file,'first')
        $first=Get-PublicInputsHash $temp @('input.txt')
        Require ($first -eq (Get-PublicInputsHash $temp @('input.txt')))
        [IO.File]::WriteAllText($file,'second')
        Require ($first -ne (Get-PublicInputsHash $temp @('input.txt')))
        MustThrow { Get-PublicInputsHash $temp @('absent') }
    }
    Check 'service python uses this copy venv' {
        $venvDir=Join-Path $temp '.venv\Scripts'
        [void][IO.Directory]::CreateDirectory($venvDir)
        $file=Join-Path $venvDir 'python.exe'
        [IO.File]::WriteAllText($file,'fixture-not-executable')
        Require ((Resolve-PublicServicePython $temp) -eq $file)
    }
    Check 'native errors propagate and cwd restored' {
        $before=(Get-Location).Path
        MustThrow { Invoke-PublicCommand 'cmd.exe' @('/d','/c','exit 9') $temp }
        Require ((Get-Location).Path -eq $before)
    }
    Check 'only stopped or healthy services may start' {
        foreach ($state in @('Stopped','Running')) { Assert-PublicStartAllowed ([pscustomobject]@{Key='chat2api';State=$state}) }
        foreach ($state in @('Conflict','Residual','Degraded','Unknown')) {
            MustThrow { Assert-PublicStartAllowed ([pscustomobject]@{Key='chat2api';State=$state}) }
        }
    }
    Check 'empty facts prove stopped' {
        $result=Get-PublicStartInspection chat2api -ListenerReader { @() } -ProcessReader { @() } -HealthChecker { throw 'unexpected health request' }
        Require ($result.State -eq 'Stopped')
    }
    Check 'unidentified port owner refused' {
        $result=Get-PublicStartInspection chat2api -ListenerReader { [pscustomobject]@{LocalPort=8000;OwningProcess=999} } -ProcessReader { @() }
        Require ($result.State -eq 'Conflict')
    }
    Check 'other checkout never adopted' {
        $result=Get-PublicStartInspection chat2api -ListenerReader { [pscustomobject]@{LocalPort=8000;OwningProcess=999} } -ProcessReader {
            [pscustomobject]@{Name='python.exe';ProcessId=999;CommandLine='python.exe "C:\fixture-other\chat2api\chat2api.py"'}
        }
        Require ($result.State -eq 'Conflict')
    }
    Check 'own process with no port is residual' {
        $script:own=[pscustomobject]@{Name='python.exe';ProcessId=999;CommandLine=('python.exe "'+(Join-Path $root 'chat2api\chat2api.py')+'"')}
        $result=Get-PublicStartInspection chat2api -ListenerReader { @() } -ProcessReader { $script:own }
        Require ($result.State -eq 'Residual')
    }
    Check 'own healthy and degraded listeners distinguished' {
        $healthy=Get-PublicStartInspection chat2api -ListenerReader { [pscustomobject]@{LocalPort=8000;OwningProcess=999} } -ProcessReader { $script:own } -HealthChecker { $true }
        $degraded=Get-PublicStartInspection chat2api -ListenerReader { [pscustomobject]@{LocalPort=8000;OwningProcess=999} } -ProcessReader { $script:own } -HealthChecker { $false }
        Require ($healthy.State -eq 'Running')
        Require ($degraded.State -eq 'Degraded')
    }
    Check 'query failures are not absence' {
        MustThrow { Get-PublicStartInspection chat2api -ListenerReader { throw 'query failed' } -ProcessReader { @() } }
        MustThrow { Get-PublicStartInspection chat2api -ListenerReader { @() } -ProcessReader { throw 'query failed' } }
    }
    Check 'HTTP errors never count as healthy' {
        Require (Test-PublicServiceHealth chat2api -Request { param($url) [pscustomobject]@{StatusCode=200} })
        foreach ($status in @(404,500,503)) {
            $script:fixtureStatus=$status
            Require (-not (Test-PublicServiceHealth chat2api -Request { param($url) [pscustomobject]@{StatusCode=$script:fixtureStatus} }))
        }
        Require (-not (Test-PublicServiceHealth chat2api -Request { throw 'network failure' }))
    }
    Check 'second start inspection refuses unhealthy without launching' {
        foreach ($state in @('Residual','Degraded','Conflict','Unknown')) {
            $script:fixtureState=$state
            $script:launches=0
            MustThrow { Start-PublicService chat2api -Inspector { [pscustomobject]@{Key='chat2api';State=$script:fixtureState} } -Launcher { $script:launches++ } -Sleeper {} }
            Require ($script:launches -eq 0)
        }
    }
    Check 'healthy start reuses without launching' {
        $result=Start-PublicService chat2api -Inspector { [pscustomobject]@{Key='chat2api';State='Running'} } -Launcher { throw 'must not launch' } -Sleeper {}
        Require ($result.Status -eq 'AlreadyRunning')
    }
    Check 'start requires owned healthy readback' {
        $script:reads=0; $script:launches=0
        $result=Start-PublicService chat2api -Inspector {
            $script:reads++
            [pscustomobject]@{Key='chat2api';State=$(if ($script:reads -eq 1) {'Stopped'} elseif ($script:reads -eq 2) {'Residual'} else {'Running'})}
        } -Launcher { $script:launches++ } -Sleeper {}
        Require ($result.Status -eq 'Started' -and $script:launches -eq 1 -and $script:reads -eq 3)
    }
    Check 'ownership race refuses readback' {
        $script:reads=0
        MustThrow { Start-PublicService chat2api -Inspector {
            $script:reads++
            [pscustomobject]@{Key='chat2api';State=$(if ($script:reads -eq 1) {'Stopped'} else {'Conflict'})}
        } -Launcher {} -Sleeper {} }
        Require ($script:reads -eq 2)
    }
    Check 'unhealthy startup remains pending without restart' {
        $script:reads=0; $script:launches=0
        $result=Start-PublicService chat2api -Inspector {
            $script:reads++
            [pscustomobject]@{Key='chat2api';State=$(if ($script:reads -eq 1) {'Stopped'} else {'Degraded'})}
        } -Launcher { $script:launches++ } -Sleeper {}
        Require ($result.Status -eq 'StartPending' -and $script:launches -eq 1)
    }
    Check 'inherited paths cleared only in current process' {
        $names=@('CHAT2API_TOKEN_FILE','USAGE_CONTROL_TOKEN_FILE','CHAT2API_LOCAL_API_KEY','AI_GATEWAY_OPENWEBUI_URL')
        $saved=@{}
        foreach ($name in $names) { $saved[$name]=[Environment]::GetEnvironmentVariable($name,'Process'); [Environment]::SetEnvironmentVariable($name,'fixture','Process') }
        try {
            Reset-PublicInheritedPaths
            Require (-not [Environment]::GetEnvironmentVariable('CHAT2API_TOKEN_FILE','Process'))
            Require (-not [Environment]::GetEnvironmentVariable('USAGE_CONTROL_TOKEN_FILE','Process'))
            Require ([Environment]::GetEnvironmentVariable('CHAT2API_LOCAL_API_KEY','Process') -eq 'fixture')
            Require ([Environment]::GetEnvironmentVariable('AI_GATEWAY_OPENWEBUI_URL','Process') -eq 'fixture')
        } finally { foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') } }
    }
    Check 'existing venv version must meet minimum' {
        Assert-PublicPythonVersion fixture -Reader { [pscustomobject]@{Output='Python 3.11.0';ExitCode=0} }
        MustThrow { Assert-PublicPythonVersion fixture -Reader { [pscustomobject]@{Output='Python 3.10.9';ExitCode=0} } }
        MustThrow { Assert-PublicPythonVersion fixture -Reader { [pscustomobject]@{Output='Python 3.14.6';ExitCode=1} } }
        MustThrow { Assert-PublicPythonVersion fixture -Reader { [pscustomobject]@{Output='unrecognizable';ExitCode=0} } }
    }
    Write-Host ('Launcher checks: '+$script:passed+' PASS')
} finally {
    $checked=[IO.Path]::GetFullPath($temp)
    if (-not $checked.StartsWith($tempBase,[StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($checked) -notlike 'AIGatewayPublicLauncherTests-*') { throw 'Unsafe test cleanup target' }
    Remove-Item -LiteralPath $checked -Recurse -Force
}
