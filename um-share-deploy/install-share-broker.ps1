# install-share-broker.ps1 - elevated install/uninstall entry for the SYSTEM
# broker host (D4 prerequisite; executed only under explicit L2/D4
# authorization, NEVER by offline tests).
#
# The implementation lives in install-share-broker-lib.ps1 (dot-sourced,
# offline-testable with injected schtasks/icacls runners). This entry only
# normalizes the parameters, asserts elevation, and runs the install or
# uninstall with the REAL command runners.
#
# Install (R41-5/R41-6 contracts enforced by the library):
#   1. verify prerequisites: admin, um-share-python.exe hardlink (owned by
#      deploy-elevated.ps1), the deployment-mode fact deploy-mode.json
#      (explicit, never inferred; projection mode also requires the derived
#      projection file) and the module closure sources. guard-status.json is
#      a per-run guard TRANSIENT - never an install prerequisite (P3/D4-P):
#      the install completes BEFORE any process start;
#   2. classify destinations BEFORE any copy: pre-existing identical hash =
#      shared (kept), different content = conflict -> REFUSE, absent ->
#      owned (copy + SHA256 verify);
#   3. create <RunRoot>\broker\ and set the protection boundary (SYSTEM/admin
#      full, <OperatorAccount> minimal RX, inheritance removed so um_share
#      keeps NO access), VERIFY the boundary by parsing icacls output, and
#      only then write broker-control-key;
#   4. write instance-channel-key into <RunRoot> root (um_share RX inherits
#      - both sides of the instance channel need it);
#   5. GENERATE run-broker.cmd from the parameters (all runtime paths derive
#      from the wrapper's own directory, so an experiment install under a
#      different root never touches production) and register the SYSTEM task
#      with the EXPLICIT trigger (-TaskTrigger; production default ONSTART,
#      experiments use ONDEMAND so nothing starts at boot);
#   6. record install-state.json (per-entry classification + hashes + the
#      namespace parameters) for a verifiable, ownership-scoped uninstall.
#
# Uninstall (-Uninstall): stop the task AND verify it is stopped/deleted
# BEFORE any file removal, then delete ONLY manifest-owned entries through a
# pure-filename whitelist; shared entries and unknown leftovers are kept.
#
# Experiment installs pass a DIFFERENT -RunRoot, -TaskName, -BrokerPipeName
# and -InstancePipePrefix; the generated artifacts then contain no
# production reference. Broker pipe SDDL and the instance owner SID stay
# empty by default: the server's placeholder SDDL then refuses to serve
# (deliberate fail-closed until L2 calibration).

param(
    [string]$RunRoot = 'C:\ProgramData\AIGatewayPublicShareRun',
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'umshare-public-broker',
    [string]$ShareTaskName = 'umshare-public-live',
    [string]$BrokerPipeName = '\\.\pipe\um-share-public-broker',
    [string]$InstancePipePrefix = '\\.\pipe\um-share-public-instance-',
    [string]$OperatorAccount = ([Security.Principal.WindowsIdentity]::GetCurrent().Name),
    [string]$InstanceOwnerSid = '',
    [string]$BrokerPipeSddl = '',
    [Parameter(Mandatory = $true)][string]$PythonHome,
    [int]$SharePort = 8100,
    [string]$ShareImage = 'um-share-python.exe',
    [string]$ExpectedOwnerAccount = 'um_share_public',
    [ValidateSet('ONSTART', 'ONDEMAND')][string]$TaskTrigger = 'ONSTART',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'install-share-broker-lib.ps1')

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'install-share-broker.ps1 requires an elevated (Administrator) session'
    }
}

function New-RealCommandRunner {
    # Real runner for schtasks/icacls: controlled list arguments, no shell,
    # structured ExitCode/Output result.
    #
    # R218 (same reproduced trap as R143 in execute-d4-experiment-lib.ps1):
    # this entry runs with ErrorActionPreference=Stop, and PS 5.1 turns a
    # native stderr line under the runner's 2>&1 into a TERMINATING
    # NativeCommandError - the preference does not reach this nested
    # scriptblock (reproduced: schtasks /Query of an ABSENT task threw
    # "系统找不到指定的文件" instead of returning the absence record, so the
    # install died with exit 1 before any structured classification). The
    # preference is set HERE: an expected stderr message stays a captured
    # record, the exit code stays the verdict, and nothing throws.
    return {
        param([string[]]$ArgumentList)
        $ErrorActionPreference = 'Continue'
        $exe = @($ArgumentList)[0]
        $rest = @($ArgumentList | Select-Object -Skip 1)
        # Sentinel: a FAILED LAUNCH (exe missing) leaves $LASTEXITCODE
        # untouched, i.e. whatever the previous native call set. Nulling it
        # first makes the verdict well-defined in every branch: a remaining
        # null is a launch failure -> explicit non-zero refusal code, never
        # a fabricated 0 success.
        $global:LASTEXITCODE = $null
        $out = & $exe @rest 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 128 }
        New-TestRunResult -ExitCode $exitCode -Output @($out)
    }
}

Assert-Admin
$cfg = New-BrokerConfig -RunRoot $RunRoot -SourceRoot $SourceRoot `
    -TaskName $TaskName -ShareTaskName $ShareTaskName `
    -BrokerPipeName $BrokerPipeName -InstancePipePrefix $InstancePipePrefix `
    -OperatorAccount $OperatorAccount -InstanceOwnerSid $InstanceOwnerSid `
    -BrokerPipeSddl $BrokerPipeSddl -PythonHome $PythonHome -SharePort $SharePort -ShareImage $ShareImage -ExpectedOwnerAccount $ExpectedOwnerAccount `
    -TaskTrigger $TaskTrigger
$runner = New-RealCommandRunner
if ($Uninstall) {
    Uninstall-Broker -Cfg $cfg -SchtasksRunner $runner -IcaclsRunner $runner
} else {
    Install-Broker -Cfg $cfg -SchtasksRunner $runner -IcaclsRunner $runner
}
