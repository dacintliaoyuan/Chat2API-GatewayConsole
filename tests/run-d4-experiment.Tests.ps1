# Static guard test for the D4 experiment preview: the delivered entry must
# refuse EVERY default/unauthorized invocation and every production-namespace
# value - it never runs install/ACL/task actions and never touches real
# services (R46.9: "written but not executed").
$ErrorActionPreference = 'Stop'
$script:failures = 0

function Assert-Refuses {
    param([scriptblock]$Block, [string]$Name, [string]$Needle)
    try {
        & $Block | Out-Null
    } catch {
        if ($Needle -and ("$($_.Exception.Message)" -notlike "*$Needle*")) {
            $script:failures++
            Write-Host "FAIL $Name`: message lacks '$Needle'" -ForegroundColor Red
            return
        }
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: script must refuse" -ForegroundColor Red
}

$entry = Join-Path $PSScriptRoot '..\um-share-deploy\run-d4-experiment.ps1'

# Common experiment parameters (the LEGAL isolated namespace).
$expRoot = Join-Path $env:TEMP ('d4-exp-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $expRoot -Force | Out-Null
$expTask = 'umshare-broker-exp'
$expPipe = '\\.\pipe\um-share-broker-exp'
$expPrefix = '\\.\pipe\um-share-instance-exp-'
$script:srcRoot = (Split-Path -Parent $PSScriptRoot)   # = ai-gateway root

# 默认调用全部拒绝（无授权开关）。
Assert-Refuses { & $entry } 'no-args refuses' 'REQUIRED'
Assert-Refuses { & $entry -ExperimentRoot $expRoot } 'missing acknowledgement refuses' 'Acknowledged'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -Stage E1 -Acknowledged } 'missing L2 authorization refuses' 'L2 authorization NOT granted'

# 生产命名空间冲突必须显式拒绝（即便带上全部开关参数）。
Assert-Refuses { & $entry -ExperimentRoot 'C:\ProgramData\UMShareRun' -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'production root refuses' 'PRODUCTION namespace'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName '\\.\pipe\um-share-broker' -InstancePipePrefix $expPrefix -SharePort 9100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'production pipe refuses' 'PRODUCTION namespace'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName 'umshare-live' -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'production task refuses' 'PRODUCTION namespace'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 8100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'production port refuses' 'fixed experiment port 9100'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9101 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'non-fixed port refuses' 'fixed experiment port 9100'
Assert-Refuses { & $entry -ExperimentRoot 'C:\ProgramData\other\..\UMShareRun' -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized } 'aliased path resolving to production refuses' 'PRODUCTION namespace'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -SourceRoot $script:srcRoot -Stage E1 -Acknowledged -L2Authorized -ItemProbe { param([string]$Path) [pscustomobject]@{ Attributes = [IO.FileAttributes]::ReparsePoint } } } 'ancestor reparse refuses' 'ancestor reparse point'
Assert-Refuses { & $entry -ExperimentRoot $expRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -Stage E1 -Acknowledged -L2Authorized } 'missing source root refuses' '-SourceRoot is REQUIRED'

# 合法实验参数 + 全开关：脚本只打印预览并退出 0（不执行任何主机动作）。
$srcRoot = $script:srcRoot
$out = & $entry -ExperimentRoot $expRoot -SourceRoot $srcRoot -TaskName $expTask -BrokerPipeName $expPipe -InstancePipePrefix $expPrefix -SharePort 9100 -Stage E1 -Acknowledged -L2Authorized 2>&1
$joined = ($out | Out-String)
if ($joined -match 'AUTHORIZED PREVIEW' -and $joined -match 'PARAMETER GUARD ONLY') {
    Write-Host 'PASS authorized preview only prints (no host action)' -ForegroundColor Green
} else {
    $script:failures++
    Write-Host 'FAIL authorized preview output' -ForegroundColor Red
}

if ($script:failures -gt 0) {
    Write-Host ("run-d4-experiment guard tests: {0} FAILURES" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'run-d4-experiment guard tests: ALL PASS' -ForegroundColor Green
exit 0
