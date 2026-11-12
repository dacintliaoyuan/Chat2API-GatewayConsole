# execute-d4-experiment.ps1 - the D4 experiment orchestration ENTRY (D4-P
# rework per handoff §62). The full lifecycle S0 -> S1 -> E1..E4 -> Cleanup
# runs through THIS entry; every OS edge is a runner/boundary that the
# offline fixture substitutes with an in-memory fake OS model (the test
# assembly path), while the production entry wires real boundaries only.
#
#   * DEFAULT (no -ExecuteExperiment): PURE PREVIEW - prints the contract,
#     isolation verdict, package hash, object inventory and stage plan.
#     ZERO writes (the only file access is reading the reviewed sources).
#   * -ExecuteExperiment: requires an ELEVATED session, a FIXED-STRUCTURE
#     config (-ConfigPath) and -ApprovedRunId EQUAL to the config's run_id
#     (the id the user approved in conversation; the switch never
#     substitutes conversational authorization).
#   * -TestHarness: the INTERNAL test assembly path - enables runner/boundary
#     injection for offline fixtures. It refuses real elevated execution
#     semantics by construction (fixtures drive the same stage functions
#     against the fake OS model); a production invocation NEVER supplies it.
#
# Isolation is checked on EVERY invocation: resolved root must be the fixed
# experiment root, never equal/inside/containing the production root, and
# neither the root nor any ancestor may be a reparse point.

param(
    [ValidateSet('Inventory', 'T0', 'T0R', 'S0', 'S1', 'E1', 'E2', 'E3', 'E4', 'Cleanup')]
    [string]$Stage = 'Inventory',
    [switch]$ExecuteExperiment,
    [string]$ApprovedRunId = '',
    [string]$ConfigPath = '',
    # §113: authorized same-SID recovery inputs (=-Stage T0R). The
    # recovery REUSES the old recorded SID; these two are required for
    # T0R and refused elsewhere.
    [string]$RecoveryOldRunId = '',
    [string]$RecoveryFixedSid = '',
    # Internal test assembly path (R62): runner/boundary injection exists
    # ONLY under -TestHarness; the production entry never accepts it.
    [switch]$TestHarness,
    [scriptblock]$SchtasksRunner,
    [scriptblock]$NetStatRunner,
    [scriptblock]$ProcessEvidenceRunner,
    # §118.1 harness-only injectable read-only edges for the recovery
    # occupancy probe (WFP dump + named-pipe enumeration) - production builds
    # the REAL edges internally and refuses caller-provided ones.
    [scriptblock]$WfpEvidenceRunner,
    [scriptblock]$PipeEvidenceRunner,
    [scriptblock]$RulesRunner,
    [scriptblock]$ProbeRunner,
    # Harness-only OS edges (R62-B/E): the fake OS file boundary and the
    # injectable broker-install / test-store assembly hooks. The production
    # entry builds the REAL boundaries internally and never accepts these
    # without -TestHarness; a real invocation therefore can never be driven
    # by arbitrary scriptblocks.
    [hashtable]$FileBoundary,
    [scriptblock]$BrokerInstaller,
    [scriptblock]$StoreFactory,
    # Harness-only E4 scenario edges (R62-E): the fault scenarios run the
    # reviewed install/recovery code on a CALLER-SCOPED root with the failure
    # injected at ONE boundary. The test assembly MUST inject an isolated
    # temp root and its failing runners; the real entry builds the real
    # fault/delegation runners internally and never accepts these without
    # -TestHarness.
    [string]$ScenarioRoot,
    [scriptblock]$IcaclsRunner,
    [scriptblock]$FailingIcaclsRunner,
    [scriptblock]$FailingSchtasksRunner,
    # §93 password-managed bootstrapping (T0): the account/credential runner
    # is an injectable OS edge like the others (harness-only; the production
    # entry builds the REAL account runner internally and never accepts this
    # without -TestHarness). The bootstrap plan and state path are T0 inputs.
    [hashtable]$AccountRunner,
    [string]$BootstrapStatePath,
    [string]$BootstrapPlanPath,
    # §95.3/95.4: the protected-store guard is an injectable OS edge like the
    # others (harness-only; real T0 builds the real mkdir+icacls guard). The
    # T0 operator identity is injectable for the SAME reason - the real
    # entry derives it from the elevated session and never accepts a
    # caller-provided one.
    [scriptblock]$BootstrapStoreGuard,
    [scriptblock]$BootstrapAclProbe,
    [string]$BootstrapOperatorAccount,
    [string]$BootstrapOperatorSid,
    [scriptblock]$ItemProbe = {
        param([string]$Path)
        if (Test-Path -LiteralPath $Path) {
            return (Get-Item -LiteralPath $Path -Force)
        }
        return $null
    }
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'execute-d4-experiment-lib.ps1')
. (Join-Path $PSScriptRoot 'd4-account-bootstrap.ps1')

function Assert-D4Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'execute-d4-experiment: -ExecuteExperiment requires an elevated (Administrator) session'
    }
}

function ConvertFrom-D4ConfigFile {
    # The config file is the fixed-structure experiment configuration.
    param([string]$Path, [hashtable]$Contract)
    if (-not $ConfigPath -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'execute-d4-experiment: real mode requires -ConfigPath (the fixed-structure experiment config)'
    }
    # UTF-8 read contract: the package's JSON artifacts are written UTF-8
    # WITHOUT a BOM, and PS 5.1 Get-Content decodes no-BOM files as ANSI/GBK
    # (a GBK double-byte pair can swallow the closing quote and corrupt the
    # JSON) - ReadAllText defaults to UTF-8, matching the writer.
    $raw = [IO.File]::ReadAllText($Path)
    $json = $null
    try {
        $json = $raw | ConvertFrom-Json
    } catch {
        throw ("experiment config is not valid JSON: " + $_.Exception.Message)
    }
    $config = @{}
    foreach ($prop in $json.PSObject.Properties) {
        if ($prop.Name -eq 'runtime_files') {
            $files = @()
            foreach ($rf in @($prop.Value)) {
                $files += @{ file = [string]$rf.file; sha256 = [string]$rf.sha256 }
            }
            $config.runtime_files = $files
        } elseif ($prop.Name -eq 'limit_rules') {
            # R67-A: each entry is an EXACT rule specification - normalized
            # to plain hashtables with EXACTLY the fixed keys (Test-D4RuleSpec
            # validates the semantics; unknown keys refuse).
            $rules = @()
            foreach ($r in @($prop.Value)) {
                $rules += @(ConvertTo-D4RuleSpec -Raw $r)
            }
            $config.limit_rules = $rules
        } else {
            $config[$prop.Name] = $prop.Value
        }
    }
    [void](Test-D4ExperimentConfig -Config $config -Contract $Contract)
    return $config
}

$contract = New-D4ExperimentContract
$sourceRoot = $contract['SourceRootDefault']
# Preview/read paths use the REAL boundary; the harness OVERRIDES it after
# the injection guard - the injected fake OS model is then the ONLY file
# system any stage touches.
$fsBoundary = New-D4RealFileBoundary

# The runtime manifest serialization (fixed structure -> canonical string) is
# part of the package identity: R62-A binds the runtime SELECTION (source
# dir + python home) AND the file closure, so changing either invalidates
# every recorded stage verdict.
$runtimeManifestJson = 'not-loaded'
$config = $null
# T0/T0R consume the bootstrap PLAN and EMIT the collected-SID config to
# -ConfigPath - the config does not exist yet, so it is never parsed here.
if ($ConfigPath -and $Stage -notin 'T0', 'T0R') {
    $config = ConvertFrom-D4ConfigFile -Path $ConfigPath -Contract $contract
    $runtimeManifestJson = ([string]$config.runtime_source_dir + '|' +
                            [string]$config.python_home + '|' +
                            ((@($config.runtime_files) | ForEach-Object {
                                $_.file + '=' + $_.sha256 }) | Sort-Object) -join ';')
}

$closure = Get-D4SourceClosureManifest -Contract $contract -SourceRoot $sourceRoot -FileBoundary $fsBoundary
$packageHash = Get-D4PackageHash -Contract $contract -ClosureManifest $closure -SourceRoot $sourceRoot -RuntimeManifestJson $runtimeManifestJson
# §95.3: the bootstrap record binds a STABLE package identity - the SOURCE
# closure only, without the runtime manifest (T0 runs before any runtime
# manifest exists; S0/S1 recompute the SAME closure identity so the record
# is never reusable across package builds yet always consistent T0 -> S0).
$bootstrapIdentity = Get-D4PackageHash -Contract $contract -ClosureManifest $closure -SourceRoot $sourceRoot -RuntimeManifestJson ''

# Isolation: string rules ALWAYS; existence/reparse probes only when the
# root exists (Inventory may run before the root is created).
$resolvedRoot = $contract['ExperimentRoot']
if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($resolvedRoot)) {
    $resolvedRoot = (Resolve-Path -LiteralPath $resolvedRoot).Path
}
[void](Test-D4Isolation -Contract $contract -ResolvedRoot $resolvedRoot -ItemProbe $ItemProbe)

if ($config) {
    $inventory = New-D4ObjectInventory -Contract $contract -ClosureManifest $closure -Config $config -PackageHash $packageHash
} else {
    $suggested = New-D4SuggestedRunId
    $inventory = $null
}

if (-not $ExecuteExperiment) {
    Write-Output ("D4-P PREVIEW (zero host writes): root=" + $resolvedRoot +
                  " suggestedRunId=" + $suggested +
                  " packageHash=" + $packageHash.Substring(0, 12) + "...")
    Write-Output 'required config slots (fixed structure): run_id, identity_source, operator_account, operator_sid, share_account, share_sid, logon_method, runtime_source_dir, runtime_files(file+sha256), python_home, broker_pipe_sddl, instance_pipe_sddl, instance_pipe_prefix, share_port(9100), probe(true), auth_mode(projection), limit_rule_ids'
    if ($inventory) {
        Write-Output ("objects: " + @($inventory['Dirs']).Count + " dirs, " + @($inventory['Files']).Count +
                      " files, tasks=" + (($inventory['Tasks'] | ForEach-Object { $_.Name }) -join ',') +
                      " pipes=" + (($inventory['Pipes'] | ForEach-Object { $_.Name }) -join ',') +
                      " port=" + $inventory['Port'])
    }
    Write-Output 'stages:'
    foreach ($s in (New-D4StagePlan)) {
        Write-Output ("  " + $s.Stage + " [" + $s.Budget + "] " + $s.Title)
    }
    Write-Output 'real mode: -ConfigPath + -ExecuteExperiment + -ApprovedRunId (admin); the approved run id must equal config.run_id.'
    exit 0
}

# ---- real/test mode dispatch ------------------------------------------------
# Execute mode (real AND harness) requires the fixed-structure config for
# every stage EXCEPT T0/T0R (the account bootstrap / authorized recovery run
# BEFORE a collected-SID config can exist; they consume the plan instead).
if ($Stage -notin 'T0', 'T0R' -and -not $config) {
    throw 'execute-d4-experiment: execute mode requires -ConfigPath (the fixed-structure experiment config)'
}
if ($TestHarness) {
    # The internal test assembly path: injected runners drive the SAME
    # stage code against the fake OS model. Injection is refused without
    # the switch, and the switch alone never substitutes authorization.
    if (-not $SchtasksRunner -or -not $FileBoundary) {
        throw 'test harness requires the injected fake OS runners/boundary'
    }
    # R67-B: S1 prepares the experiment DACL matrix - the harness MUST
    # inject the icacls edge too (the real icacls never runs offline).
    if (-not $IcaclsRunner) {
        throw 'test harness requires the injected icacls runner (the ACL edge is never real offline)'
    }
    # R62-B/E: the fixture MUST also inject the broker-install and test-store
    # hooks - the real install/store paths are NEVER executed offline.
    if (-not $BrokerInstaller -or -not $StoreFactory) {
        throw 'test harness requires the injected broker-install and store hooks (real OS paths never run offline)'
    }
    # §93/§97.3: T0 and any password-managed stage ALSO need the injected
    # account/credential runner, the protected-store guard AND the ACL probe
    # - the real LocalAccounts/ScheduledTasks APIs and the real DACL reads
    # are never reachable offline (missing injection = fail closed, zero
    # calls).
    if ($Stage -in 'T0', 'T0R' -and (-not $AccountRunner -or -not $BootstrapStoreGuard -or -not $BootstrapAclProbe)) {
        throw 'test harness T0/T0R requires the injected account/credential runner, protected-store guard AND ACL probe'
    }
    if ($Stage -eq 'T0R' -and (-not $WfpEvidenceRunner -or -not $PipeEvidenceRunner)) {
        throw 'test harness T0R requires the injected WFP and pipe evidence edges (the recovery occupancy probe needs them)'
    }
    if ($null -ne $config -and [string]$config.logon_method -eq 'password-managed' -and
            (-not $AccountRunner -or -not $BootstrapAclProbe)) {
        throw 'test harness password-managed stage requires the injected account runner AND ACL probe (fail-closed)'
    }
    $fsBoundary = $FileBoundary
} else {
    if ($SchtasksRunner -or $NetStatRunner -or $ProcessEvidenceRunner -or
            $WfpEvidenceRunner -or $PipeEvidenceRunner -or
            $RulesRunner -or $ProbeRunner -or $FileBoundary -or
            $BrokerInstaller -or $StoreFactory -or $ScenarioRoot -or
            $IcaclsRunner -or $FailingIcaclsRunner -or $FailingSchtasksRunner -or
            $AccountRunner -or $BootstrapStoreGuard -or $BootstrapAclProbe) {
        throw 'runner/boundary injection is the internal test assembly path only (-TestHarness); the production entry builds real ones'
    }
    if ($Stage -in 'T0', 'T0R') {
        if (-not $BootstrapPlanPath) {
            throw 'execute-d4-experiment: real T0/T0R requires -BootstrapPlanPath (the fixed-structure bootstrap plan JSON)'
        }
        if ($BootstrapOperatorAccount -or $BootstrapOperatorSid) {
            throw 'execute-d4-experiment: the T0/T0R operator identity is derived from the elevated session, never caller-provided'
        }
        if ($Stage -eq 'T0R' -and (-not $RecoveryOldRunId -or -not $RecoveryFixedSid)) {
            throw 'execute-d4-experiment: real T0R requires -RecoveryOldRunId and -RecoveryFixedSid (the fixed recorded SID is reused, never guessed)'
        }
        # §95.3: the bootstrap record path enforcement for the WHOLE real
        # dispatch lives after the run-id check (every stage, not only T0).
    } elseif (-not $ConfigPath) {
        throw 'execute-d4-experiment: real mode requires -ConfigPath (the fixed-structure experiment config)'
    }
    Assert-D4Admin
    $runner = New-D4RealNetStatRunner
    $SchtasksRunner = $runner
    $NetStatRunner = $runner
    $ProcessEvidenceRunner = $runner
    $icaclsRunner = $runner
    # R67-A: the REAL rules runner carries the reviewed rule specs INSIDE
    # its closure (never free parameters). R78-D: the FIXED contract travels
    # with it (the permit scope expectation is a contract fact, never an
    # implicit empty dependency inside the runner).
    # §103 real-T0 wiring: the rules/probe runners consume the COLLECTED
    # config, which does not exist until T0 emits it - build them only for
    # stages that have one (T0 uses neither)
    if ($null -ne $config) {
        $RulesRunner = New-D4RealRulesRunner -RuleSpecs @($config.limit_rules) -Contract $contract
        $ProbeRunner = New-D4RealProbeRunner -Contract $contract -Config $config
    }
    # §93: the REAL account/credential runner (LocalAccounts + ScheduledTasks
    # cmdlets, in-process). Constructing it makes NO OS calls - only the
    # invoked ops do, and those only from the T0/password-managed branches.
    $AccountRunner = New-D4AccountRunner
    # §95.3/§97.3: the REAL protected-store guard (existing store = read-only
    # verify; absent store = protected creation) and the REAL ACL probe that
    # feeds the shared read-only trust gate.
    # §99.2: the ONLY OS access is Get-Acl (the injectable edge); the owner
    # translation and the rule extraction are the REAL structured code.
    $BootstrapGetAclEdge = { param([string]$Path) Get-Acl -LiteralPath $Path }
    $BootstrapAclProbe = New-D4RealAclProbe -GetAclEdge $BootstrapGetAclEdge
    $BootstrapStoreGuard = New-D4RealBootstrapStoreGuard -RootDir $contract['BootstrapRoot'] -NativeRunner $runner `
        -AclProbe $BootstrapAclProbe -StoreProbe $ItemProbe -FileBoundary $fsBoundary
    # §118.1: the REAL read-only evidence edges for the recovery occupancy
    # probe (WFP dump via netsh + named-pipe enumeration) - built internally,
    # never caller-provided in production. §130: both now run the reviewed
    # runner closures from the lib (full-argv contract, exit code AFTER the
    # call, stderr never inside the XML payload, real \\.\pipe\ namespace).
    $WfpEvidenceRunner = New-D4RealWfpEvidenceRunner
    $PipeEvidenceRunner = New-D4RealPipeEvidenceRunner
    $fsBoundary = New-D4RealFileBoundary
    # The REAL E4 fault edges are fixed-structure delegation runners: the
    # failing icacls runner fails exactly the FIRST ACL-set call (the audited
    # fault point) and delegates everything else to the real icacls; the
    # failing schtasks runner fails the FIRST /Create and delegates the rest.
    $IcaclsRunner = $icaclsRunner
    # The fault flags live in per-instance hashtables captured by the
    # closures - never in $script: scope, which would leak across separate
    # entries invocations in a dot-sourced fixture.
    $aclFault = @{ fired = $false }
    $FailingIcaclsRunner = {
        param([string[]]$ArgumentList)
        $joined = ($ArgumentList -join ' ')
        if ($joined -match '/grant' -and -not $aclFault['fired']) {
            $aclFault['fired'] = $true
            return (New-TestRunResult -ExitCode 1 -Output @('INJECTED FAULT: acl change boundary'))
        }
        return (Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList $ArgumentList)
    }.GetNewClosure()
    $createFault = @{ fired = $false }
    $FailingSchtasksRunner = {
        param([string[]]$ArgumentList)
        $action = ($ArgumentList | Select-Object -Skip 1 | Select-Object -First 1)
        if ($action -eq '/Create' -and -not $createFault['fired']) {
            $createFault['fired'] = $true
            return (New-TestRunResult -ExitCode 1 -Output @('INJECTED FAULT: task register boundary'))
        }
        return (Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList $ArgumentList)
    }.GetNewClosure()
}
if (-not $ApprovedRunId) {
    throw 'execute-d4-experiment: real mode requires -ApprovedRunId (the conversation-approved id)'
}
[void](Test-D4RunIdFormat $ApprovedRunId)
# §95.3: the bootstrap record path is FIXED for EVERY real stage (T0 writes
# it; S0/S1 read it for the run-owned exemption) - a caller-chosen override
# is the harness-only path, refused here ONCE for the whole real dispatch.
if (-not $TestHarness -and $BootstrapStatePath) {
    $derivedBootstrapPath = Get-D4BootstrapStatePath -Contract $contract -RunId $ApprovedRunId
    if ($BootstrapStatePath.TrimEnd('\') -ne $derivedBootstrapPath.TrimEnd('\')) {
        throw ('execute-d4-experiment: real mode rejects a custom bootstrap state path (fixed protected store only): ' + $BootstrapStatePath)
    }
}
if ($null -ne $config -and [string]$config.run_id -ne $ApprovedRunId) {
    throw ("execute-d4-experiment: -ApprovedRunId '" + $ApprovedRunId +
           "' does not equal the config run_id '" + $config.run_id +
           "'; the approved id is THE run identity everywhere")
}
# One run id from the entry onward (R62-A): inventory, state path, records.
# (T0 runs BEFORE a collected-SID config exists - the config slots stay
# null and the bootstrap plan carries the run id instead.)
if ($null -ne $config) { $config.run_id = $ApprovedRunId }
if ($null -ne $config) {
    $inventory = New-D4ObjectInventory -Contract $contract -ClosureManifest $closure -Config $config -PackageHash $packageHash
    $statePath = Get-D4StageStatePath -Contract $contract -PackageHash $packageHash -RunId $ApprovedRunId
    $ledgerPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $packageHash -RunId $ApprovedRunId
    $evidenceDir = Join-Path $resolvedRoot $contract['StateDirRel']
} else {
    $inventory = $null
    $statePath = $null
    $ledgerPath = $null
    $evidenceDir = $null
}
if (-not $BootstrapStatePath) {
    $BootstrapStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $ApprovedRunId
}
# §95.5: THIS run's own share task (registered by T0) is part of this run's
# namespace for S0/S1 ONLY with a FULL trusted proof: the bootstrap record
# (run-id + package bound) AND the record SID matching the CONFIG share_sid
# AND the full task spec (exact command + Password + root TaskPath) that the
# shared shape verifier will enforce. Used by BOTH namespace checks below;
# $null for every other logon method keeps the existing occupancy semantics.
$ownShare = $null
if ($null -ne $config -and [string]$config.logon_method -eq 'password-managed') {
    # §97.3: the SAME read-only trust gate guards EVERY record consumer - an
    # untrusted store (foreign write ACE, reparse, query failure) grants NO
    # exemption at all.
    $storeTrustOwn = Test-D4BootstrapStoreTrust -BootstrapRoot $contract['BootstrapRoot'] `
        -StatePath $BootstrapStatePath -AclProbe $BootstrapAclProbe -StoreProbe $ItemProbe `
        -FileBoundary $fsBoundary -CheckLeaf
    if (-not $storeTrustOwn.trusted) {
        Write-Log ("password-managed bootstrap store is NOT trusted; no run-owned task exemption: " +
                   $storeTrustOwn.reason)
    } else {
    # §97.4: the collected config must correspond to the record's IMMUTABLE
    # plan - the plan identity is recomputed from the CONFIG and compared;
    # a mismatch grants no exemption (S0/S1 stay occupied/refuse).
    $cfgRuleMap = Get-D4RuleRoleMap -Rules @($config.limit_rules)
    $cfgPlanIdentity = Get-D4BootstrapPlanIdentity -RunId $ApprovedRunId `
        -AccountName ([string]$config.share_account) -TaskName ([string]$contract['ShareTaskName']) `
        -PythonHome ([string]$config.python_home) -RuntimeSourceDir ([string]$config.runtime_source_dir) `
        -RuntimeFiles @($config.runtime_files) -RuleMap $cfgRuleMap
    try {
    $bsOwn = Read-D4BootstrapState -StatePath $BootstrapStatePath -RunId $ApprovedRunId `
        -AccountName ([string]$config.share_account) -TaskName ([string]$contract['ShareTaskName']) `
        -PackageHash $bootstrapIdentity -PlanIdentity $cfgPlanIdentity -FileBoundary $fsBoundary
    } catch {
        # A record that fails ANY binding (including the plan identity) is not
        # this run's trusted record - no exemption, original bytes kept.
        Write-Log ("password-managed bootstrap record does not match the collected config plan; no run-owned task exemption: " +
                   $_.Exception.Message)
        $bsOwn = $null
    }
    if ($null -ne $bsOwn -and [string]$bsOwn.status -eq 'task-registered' -and [string]$bsOwn.created_sid) {
        if ([string]$bsOwn.created_sid -ne [string]$config.share_sid) {
            # The config's share identity and the bootstrap record disagree -
            # the exemption is never granted (S0/S1 stay occupied/refuse).
            Write-Log ("password-managed bootstrap record SID " + [string]$bsOwn.created_sid +
                       " does not match the config share_sid " + [string]$config.share_sid +
                       "; no run-owned task exemption")
        } else {
            $ownShare = @{
                Name = [string]$contract['ShareTaskName']
                IdentitySid = [string]$bsOwn.created_sid
                LogonType = 'Password'
                Command = (Join-Path $resolvedRoot 'run-live.cmd')
                TaskPath = ('\' + [string]$contract['ShareTaskName'])
                Trigger = 'ONDEMAND'
            }
        }
    }
    if ($null -eq $ownShare) {
        # §113/§116.2: a COMPLETED recovery record for THIS run grants the
        # same exemption ONLY when its OWN leaf passes the SAME read-only
        # trust gate (a different leaf's trust is never reused) - then the
        # plan identity + fixed SID + full shape are enforced.
        $recTrustOwn = Test-D4BootstrapStoreTrust -BootstrapRoot $contract['BootstrapRoot'] `
            -StatePath (Get-D4RecoveryStatePath -Contract $contract -RunId $ApprovedRunId) `
            -AclProbe $BootstrapAclProbe -StoreProbe $ItemProbe `
            -FileBoundary $fsBoundary -CheckLeaf
        if (-not $recTrustOwn.trusted) {
            Write-Log ("recovery record leaf is NOT trusted; no run-owned task exemption: " + $recTrustOwn.reason)
        } else {
            try {
                $recOwn = Read-D4RecoveryState -StatePath (Get-D4RecoveryStatePath -Contract $contract -RunId $ApprovedRunId) `
                    -RunId $ApprovedRunId -AccountName ([string]$config.share_account) `
                    -TaskName ([string]$contract['ShareTaskName']) `
                    -PackageHash $bootstrapIdentity -PlanIdentity $cfgPlanIdentity `
                    -FileBoundary $fsBoundary
            } catch {
                Write-Log ("recovery record does not match the collected config plan; no run-owned task exemption: " + $_.Exception.Message)
                $recOwn = $null
            }
            if ($null -ne $recOwn -and [string]$recOwn.status -eq 'task-registered' -and
                    [string]$recOwn.created_sid -eq [string]$config.share_sid) {
                $ownShare = @{
                    Name = [string]$contract['ShareTaskName']
                    IdentitySid = [string]$recOwn.created_sid
                    LogonType = 'Password'
                    Command = (Join-Path $resolvedRoot 'run-live.cmd')
                    TaskPath = ('\' + [string]$contract['ShareTaskName'])
                    Trigger = 'ONDEMAND'
                }
            }
        }
    }
}
    }
switch ($Stage) {
    # The T0/T0R cases SHARE one dispatch body; PS 5.1 rejects comma
    # separated switch labels, so the condition is an explicit scriptblock.
    { $Stage -eq 'T0' -or $Stage -eq 'T0R' } {
        $recoveryMode = ($Stage -eq 'T0R')
        if ($recoveryMode) {
            # §113: recovery reuses the OLD recorded SID - never guessed;
            # the recovery old run id must be a legal run id.
            if (-not $RecoveryOldRunId -or -not $RecoveryFixedSid) {
                throw 'T0R requires -RecoveryOldRunId and -RecoveryFixedSid (the fixed recorded SID is reused, never guessed)'
            }
            [void](Test-D4RunIdFormat $RecoveryOldRunId)
            if ($RecoveryFixedSid -notmatch '^S-1-5-\d+(-\d+)*$') {
                throw 'T0R refuses a malformed -RecoveryFixedSid'
            }
        }
        # §93 + §95.4: ONE elevated deployment process for the dedicated
        # experiment account + the password-managed share task registration.
        # No experiment config exists before the account SID is known - T0
        # consumes the fixed bootstrap plan (rule GUIDs are a FIXED per-run
        # input, never regenerated), creates the account, registers the share
        # task with an ephemeral in-process credential, and EMITS the
        # collected-SID experiment config that S0+ stages run on.
        # §95.4: EVERY check that can fail without host side effects runs
        # BEFORE the account bootstrap - a missing/later-failing -ConfigPath
        # must cost ZERO CreateAccount calls.
        if (-not $BootstrapPlanPath) {
            throw 'T0 requires -BootstrapPlanPath (the fixed-structure bootstrap plan JSON)'
        }
        if (-not $ConfigPath) {
            throw 'T0 requires -ConfigPath as the OUTPUT location for the emitted experiment config'
        }
        # §95.4: the output path is validated BEFORE any account action - a
        # non-JSON or foreign-run file at the output location refuses with
        # ZERO CreateAccount calls (a same-run config may be a resume; the
        # final content equality decides at write time).
        if (Invoke-D4File -Boundary $fsBoundary -Operation 'IsLeaf' -Arguments @($ConfigPath)) {
            $existingRaw = [string](Invoke-D4File -Boundary $fsBoundary -Operation 'ReadText' -Arguments @($ConfigPath))
            $existingObj = $null
            try { $existingObj = $existingRaw | ConvertFrom-Json } catch {
                throw ("T0 precheck refused: the config output path holds a non-JSON file (no clobber): " + $ConfigPath)
            }
            if ($null -eq $existingObj -or $null -eq $existingObj.PSObject.Properties['run_id'] -or
                    [string]$existingObj.run_id -ne $ApprovedRunId) {
                throw ("T0 precheck refused: the config output path holds a DIFFERENT run's config (no clobber): " + $ConfigPath)
            }
        }
        $plan = Read-D4BootstrapPlan -Path $BootstrapPlanPath -FileBoundary $fsBoundary `
            -AllowedAccount ([string]$contract['BootstrapAccountDefault'])
        if ($plan['run_id'] -ne $ApprovedRunId) {
            throw ("T0: bootstrap plan run_id '" + $plan['run_id'] +
                   "' does not equal -ApprovedRunId '" + $ApprovedRunId + "'")
        }
        # Operator identity: harness injects it (offline fixtures must not
        # leak the real elevated identity into a config artifact); the real
        # elevated session derives it and caller-provided values were already
        # rejected in the dispatch block.
        if ($TestHarness) {
            if (-not $BootstrapOperatorAccount -or -not $BootstrapOperatorSid) {
                throw 'test harness T0 requires the injected operator identity (never the real session identity offline)'
            }
            $opAccount = $BootstrapOperatorAccount
            $opSid = $BootstrapOperatorSid
        } else {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $opAccount = $identity.Name.Split('\')[-1]
            $opSid = $identity.User.Value
        }
        # Read-only occupancy precheck: the account bootstrap must not start
        # into a namespace this run does not own (tasks / port / root).
        # §95.4: a pre-registered share task is RECOVERABLE only with the
        # FULL trusted proof - THIS run's task-registered bootstrap record
        # (run+package bound) AND the complete definition shape (exact
        # command + Password + root path) via the shared verifier. Anything
        # else stays a foreign occupant and refuses with zero actions.
        foreach ($tn in @($contract['BrokerTaskName'], $contract['ShareTaskName'])) {
            $q = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
                'schtasks', '/Query', '/TN', $tn)
            $txt = (@($q.Output) -join "`n")
            if ($q.ExitCode -eq 0) {
                $preOwn = $false
                if ($tn -eq [string]$contract['ShareTaskName']) {
                    # §97.3: the SAME read-only trust gate guards the T0
                    # recovery read - an untrusted store NEVER yields the
                    # run-owned exemption.
                    $preTrust = Test-D4BootstrapStoreTrust -BootstrapRoot $contract['BootstrapRoot'] `
                        -StatePath $BootstrapStatePath -AclProbe $BootstrapAclProbe -StoreProbe $ItemProbe `
                        -FileBoundary $fsBoundary -CheckLeaf
                    if ($preTrust.trusted) {
                        $bsPre = $null
                        try {
                            $prePlanIdentity = Get-D4BootstrapPlanIdentity -RunId $ApprovedRunId `
                                -AccountName ([string]$plan['account_name']) -TaskName ([string]$contract['ShareTaskName']) `
                                -PythonHome ([string]$plan['python_home']) -RuntimeSourceDir ([string]$plan['runtime_source_dir']) `
                                -RuntimeFiles $plan['runtime_files'] `
                                -RuleMap (Get-D4RuleRoleMap -OrderedIds ([string[]]$plan['rule_ids']))
                            $bsPre = Read-D4BootstrapState -StatePath $BootstrapStatePath -RunId $ApprovedRunId `
                                -AccountName ([string]$plan['account_name']) -TaskName ([string]$contract['ShareTaskName']) `
                                -PackageHash $bootstrapIdentity -PlanIdentity $prePlanIdentity `
                                -FileBoundary $fsBoundary
                        } catch { $bsPre = $null }
                        if ($null -ne $bsPre -and [string]$bsPre.status -eq 'task-registered' -and [string]$bsPre.created_sid) {
                            $preSpec = @{ Name = [string]$tn; IdentitySid = [string]$bsPre.created_sid;
                                          LogonType = 'Password';
                                          Command = (Join-Path $contract['ExperimentRoot'] 'run-live.cmd');
                                          TaskPath = '\'; Trigger = 'ONDEMAND' }
                            $preOwn = Test-D4NamespaceOwnedTask -TaskSpec $preSpec -Trigger 'ONDEMAND' `
                                -SchtasksRunner $SchtasksRunner
                        }
                    } else {
                        Write-Log ("T0 precheck: bootstrap store NOT trusted; no resume exemption: " + $preTrust.reason)
                    }
                    if (-not $preOwn -and $recoveryMode) {
                        # §113: a COMPLETED recovery record for THIS run
                        # grants the same exemption - trusted store, plan
                        # identity, fixed SID, full shape.
                        $preTrustR = Test-D4BootstrapStoreTrust -BootstrapRoot $contract['BootstrapRoot'] `
                            -StatePath (Get-D4RecoveryStatePath -Contract $contract -RunId $ApprovedRunId) `
                            -AclProbe $BootstrapAclProbe -StoreProbe $ItemProbe `
                            -FileBoundary $fsBoundary -CheckLeaf
                        if ($preTrustR.trusted) {
                            $recPre = $null
                            try {
                                $recPlanId = Get-D4BootstrapPlanIdentity -RunId $ApprovedRunId `
                                    -AccountName ([string]$plan['account_name']) -TaskName ([string]$contract['ShareTaskName']) `
                                    -PythonHome ([string]$plan['python_home']) -RuntimeSourceDir ([string]$plan['runtime_source_dir']) `
                                    -RuntimeFiles $plan['runtime_files'] `
                                    -RuleMap (Get-D4RuleRoleMap -OrderedIds ([string[]]$plan['rule_ids']))
                                $recPre = Read-D4RecoveryState -StatePath (Get-D4RecoveryStatePath -Contract $contract -RunId $ApprovedRunId) `
                                    -RunId $ApprovedRunId -AccountName ([string]$plan['account_name']) `
                                    -TaskName ([string]$contract['ShareTaskName']) `
                                    -PackageHash $bootstrapIdentity -PlanIdentity $recPlanId `
                                    -FileBoundary $fsBoundary
                            } catch { $recPre = $null }
                            if ($null -ne $recPre -and [string]$recPre.status -eq 'task-registered' -and
                                    [string]$recPre.created_sid -eq $RecoveryFixedSid) {
                                $preSpecR = @{ Name = [string]$tn; IdentitySid = [string]$RecoveryFixedSid;
                                               LogonType = 'Password';
                                               Command = (Join-Path $contract['ExperimentRoot'] 'run-live.cmd');
                                               TaskPath = '\'; Trigger = 'ONDEMAND' }
                                $preOwn = Test-D4NamespaceOwnedTask -TaskSpec $preSpecR -Trigger 'ONDEMAND' `
                                    -SchtasksRunner $SchtasksRunner
                            }
                        }
                    }
                }
                if (-not $preOwn) {
                    throw ("T0 precheck refused: task already present without this run's trusted record: " + $tn)
                }
                continue   # THIS run's own registered task (trusted record + full shape) - resume allowed
            }
            if (-not ($txt -match 'cannot find|找不到')) {
                throw ("T0 precheck unconfirmed: task query failed (not provably absent): " + $tn +
                       " (exit " + $q.ExitCode + ")")
            }
        }
        if ($NetStatRunner) {
            $net = Invoke-CommandRunner -Runner $NetStatRunner -ArgumentList @(
                'netstat', '-ano', '-p', 'TCP')
            if ($net.ExitCode -ne 0) {
                throw ("T0 precheck refused: port check failed (exit " + $net.ExitCode + ")")
            }
            $listen = @($net.Output | Where-Object { $_ -match ':\s*9100\s' -and $_ -match 'LISTENING' })
            if ($listen.Count -gt 0) {
                throw 'T0 precheck refused: port 9100 already has a listener'
            }
        }
        if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($resolvedRoot)) {
            throw ("T0 precheck refused: experiment root already exists: " + $resolvedRoot)
        }
        if ($recoveryMode) {
            # §118.1: the recovery precheck task gate aligns with the FULL-
            # ENUMERATION tri-state - an UNCONFIRMED QueryTask verdict is
            # never treated as absence, even if a keyword query said absent.
            foreach ($tnP in @([string]$contract['BrokerTaskName'], [string]$contract['ShareTaskName'])) {
                $qp = & $AccountRunner['QueryTask'] $tnP @{}
                if ($qp.present -eq $null) {
                    throw ("T0R precheck unconfirmed: task '" + $tnP + "' query is UNCONFIRMED (" + [string]$qp.error + "); refusing (never 'absent' on a query failure)")
                }
            }
        }
        # The definition builder is bound to the CREATED SID at bootstrap time
        # (never a pre-creation guess) - Password logon, fixed wrapper path.
        $defBuilder = {
            param([string]$Sid)
            New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $Sid } `
                -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') `
                -LogonType 'Password'
        }.GetNewClosure()
        # §97.4: the IMMUTABLE plan identity is computed from the FIXED run
        # plan and bound into the record BEFORE creation-intent; a changed
        # plan refuses the same run before any side effect.
        $planRuleMap = Get-D4RuleRoleMap -OrderedIds ([string[]]$plan['rule_ids'])
        $planIdentity = Get-D4BootstrapPlanIdentity -RunId $ApprovedRunId `
            -AccountName ([string]$plan['account_name']) -TaskName ([string]$contract['ShareTaskName']) `
            -PythonHome ([string]$plan['python_home']) -RuntimeSourceDir ([string]$plan['runtime_source_dir']) `
            -RuntimeFiles $plan['runtime_files'] -RuleMap $planRuleMap
        if ($recoveryMode) {
            # §113: the authorized same-SID recovery path (no account
            # creation; the fixed recorded SID + old creation record + a
            # currently-disabled non-admin account + clean occupancy are
            # all re-verified read-only inside the recovery entry).
            # §116.2: the frozen recovery PLAN section is the authoritative
            # old-source baseline (never the same read being verified) - it
            # must match the recovery parameters and carry the 64-hex hash.
            $recSec = $null
            try { $recSec = $plan['recovery'] } catch { $recSec = $null }
            if ($null -eq $recSec -or
                    [string]$recSec['old_run_id'] -ne $RecoveryOldRunId -or
                    [string]$recSec['fixed_sid'] -ne $RecoveryFixedSid -or
                    [string]$recSec['created_state_sha256'] -notmatch '^[0-9a-fA-F]{64}$') {
                throw 'T0R: the frozen recovery plan section does not match -RecoveryOldRunId/-RecoveryFixedSid or lacks the 64-hex created_state_sha256'
            }
            $expectedOldSha = [string]$recSec['created_state_sha256']
            $oldStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $RecoveryOldRunId
            $recoveryStatePath = Get-D4RecoveryStatePath -Contract $contract -RunId $ApprovedRunId
            # Harness: the REAL ItemProbe would interrogate the REAL protected
            # store paths (which exist on this host) - use a no-op store probe
            # so the offline recovery trusts only the injected boundaries.
            $recoveryStoreProbe = $ItemProbe
            if ($TestHarness) { $recoveryStoreProbe = { param([string]$Path) return $null } }
            # §116.3: the occupancy probe is TRI-STATE - a query failure is
            # UNCONFIRMED (never 'clean'), a hard present stays present, and
            # absence requires a completed query with zero matches. Tasks use
            # the reviewed single-task semantics; processes use the reviewed
            # Get-D4ProcessEvidence adapter; WFP requires a successful,
            # parseable dump with per-GUID explicit absence.
            $occProbe = {
                # §118.1: FULL tri-state occupancy - every datum must be a
                # COMPLETED query or a hard present; any query failure is
                # unconfirmed (never 'clean'), including queries whose message
                # happens to contain 'cannot find/找不到'.
                $oc = @(); $clean = $true
                foreach ($tnO in @([string]$contract['BrokerTaskName'], [string]$contract['ShareTaskName'])) {
                    if (-not $AccountRunner -or -not $AccountRunner.ContainsKey('QueryTask')) {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += ('task-runner-MISSING: ' + $tnO)
                        continue
                    }
                    $qO = & $AccountRunner['QueryTask'] $tnO @{}
                    if ($qO.present -eq $null) {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += ('task-query-UNCONFIRMED: ' + $tnO + ' (' + [string]$qO.error + ')')
                    } elseif ($qO.present -eq $true) {
                        $clean = $false; $oc += ('task-present: ' + $tnO)
                    }
                }
                if (-not $NetStatRunner) {
                    if ($clean -ne $false) { $clean = $null }
                    $oc += 'netstat-runner-MISSING'
                } else {
                    $netO = Invoke-CommandRunner -Runner $NetStatRunner -ArgumentList @('netstat', '-ano', '-p', 'TCP')
                    if ($netO.ExitCode -ne 0) {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += 'netstat-query-FAILED'
                    } else {
                        # §120.1/§122/§124: netstat rows are validated by the
                        # shared, column-strict Test-D4NetstatRowFacts (exactly
                        # five whitespace columns: TCP, validated IPv4/[IPv6]/
                        # wildcard address+port 0-65535, a KNOWN TCP state,
                        # numeric PID). ANY unknown or invalid non-empty line
                        # (even mixed among legal rows) makes the WHOLE output
                        # UNCONFIRMED - a bad row can never be swallowed. exit 0
                        # with genuinely empty output stays the legal 'no
                        # listener' convention.
                        $sawBadNet = $false; $netHit = @()
                        foreach ($lnO in @($netO.Output)) {
                            $sO = [string]$lnO
                            if ([string]::IsNullOrWhiteSpace($sO)) { continue }
                            $tO = $sO.Trim()
                            if ($tO -eq 'Active Connections' -or $tO -eq '活动连接' -or $tO -match '^(Proto|协议)\s') {
                                # recognized header line
                                continue
                            }
                            $netFacts = Test-D4NetstatRowFacts -Row $tO
                            if ($netFacts.Legal) {
                                if ($netFacts.Port9100Listening) { $netHit += $sO }
                            } else {
                                $sawBadNet = $true
                            }
                        }
                        if ($sawBadNet) {
                            if ($clean -ne $false) { $clean = $null }
                            $oc += 'netstat-output-BAD (unknown or field-invalid line)'
                        } elseif ($netHit.Count -gt 0) {
                            $clean = $false; $oc += 'port-9100-listening'
                        }
                    }
                }
                if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($resolvedRoot)) {
                    $clean = $false; $oc += 'experiment-root-exists'
                }
                if (-not $ProcessEvidenceRunner) {
                    if ($clean -ne $false) { $clean = $null }
                    $oc += 'process-runner-MISSING'
                } else {
                    $evO = Get-D4ExperimentProcessEvidence `
                        -ExperimentRoot $contract['ExperimentRoot'] -Runner $ProcessEvidenceRunner
                    if ($evO.Unconfirmed) {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += 'process-evidence-UNCONFIRMED'
                    } elseif ($evO.Pids.Count -gt 0) {
                        $clean = $false; $oc += ('experiment-process-present pids=' + ($evO.Pids -join ','))
                    }
                }
                if (-not $PipeEvidenceRunner) {
                    if ($clean -ne $false) { $clean = $null }
                    $oc += 'pipe-runner-MISSING'
                } else {
                    try {
                        $pipesO = @(& $PipeEvidenceRunner)
                        if ((@($pipesO | Where-Object { $_ -match '(?i)um-share-(broker|instance)' })).Count -gt 0) {
                            $clean = $false; $oc += 'experiment-pipe-present'
                        }
                    } catch {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += 'pipe-enumeration-UNCONFIRMED'
                    }
                }
                if (-not $WfpEvidenceRunner) {
                    if ($clean -ne $false) { $clean = $null }
                    $oc += 'wfp-runner-MISSING'
                } else {
                    $wpO = Get-D4WfpRuleEvidence -Runner $WfpEvidenceRunner -RuleIds ([string[]]$plan['rule_ids'])
                    if ($wpO.Unconfirmed) {
                        if ($clean -ne $false) { $clean = $null }
                        $oc += ('wfp-query-UNCONFIRMED: ' + [string]$wpO.Reason)
                    } else {
                        foreach ($pO in @($wpO.Present)) { $clean = $false; $oc += ('rule-present: ' + [string]$pO) }
                    }
                }
                return @{ clean = $clean; detail = ($oc -join '; ') }
            }.GetNewClosure()
            $rec = Invoke-D4AccountRecovery -Contract $contract -RunId $ApprovedRunId `
                -AccountName $plan['account_name'] -ShareTaskName $contract['ShareTaskName'] `
                -OldRunId $RecoveryOldRunId -OldStatePath $oldStatePath -FixedSid $RecoveryFixedSid `
                -ExpectedOldStateSha256 $expectedOldSha `
                -DefinitionBuilder $defBuilder -PackageHash $bootstrapIdentity -PlanIdentity $planIdentity `
                -RecoveryStatePath $recoveryStatePath -AccountRunner $AccountRunner `
                -FileBoundary $fsBoundary -StoreProbe $recoveryStoreProbe -AclProbe $BootstrapAclProbe `
                -OccupancyProbe $occProbe
            if (-not $rec.Ok) {
                throw ("T0R recovery " + $rec.Refused + ": " + $rec.Reason)
            }
            if ([string]$rec.Status -ne 'task-registered') {
                # §120.2: an already-DISABLED (or other non-task-registered)
                # success is a CLOSED terminal, NOT this run's recovery
                # success - zero config writes, zero re-enable/register;
                # existing config bytes are never overwritten.
                throw ("T0R refuse: the recovery did not reach task-registered (status '" + [string]$rec.Status + "'); no config emission, no re-enable/register")
            }
            $bootSid = [string]$rec.CreatedSid
        } else {
            $boot = Invoke-D4AccountBootstrap -Contract $contract -RunId $ApprovedRunId `
                -AccountName $plan['account_name'] -ShareTaskName $contract['ShareTaskName'] `
                -DefinitionBuilder $defBuilder -PackageHash $bootstrapIdentity `
                -StatePath $BootstrapStatePath -AccountRunner $AccountRunner `
                -FileBoundary $fsBoundary -StoreGuard $BootstrapStoreGuard -StoreProbe $ItemProbe `
                -AclProbe $BootstrapAclProbe -PlanIdentity $planIdentity
            if (-not $boot.Ok) {
                throw ("T0 bootstrap " + $boot.Refused + ": " + $boot.Reason)
            }
            $bootSid = [string]$boot.CreatedSid
        }
        try {
        # Emit the collected-SID experiment config. The rule GUIDs come from
        # the FIXED run plan (never regenerated) - a recovery re-emits the
        # SAME config for the same run id (§95.4).
        $cfg = New-D4ExperimentConfig -Contract $contract -RunId $ApprovedRunId `
            -OperatorAccount $opAccount -OperatorSid $opSid `
            -ShareAccount $plan['account_name'] -ShareSid $bootSid `
            -PythonHome $plan['python_home'] -RuntimeSourceDir $plan['runtime_source_dir'] `
            -RuntimeFiles $plan['runtime_files'] -RuleIds $plan['rule_ids']
        [void](Test-D4ExperimentConfig -Config $cfg -Contract $contract)
        $cfgJson = $cfg | ConvertTo-Json -Depth 6
        # No-clobber emission: a same-content file is a legitimate resume; a
        # DIFFERENT content is never overwritten (§95.4).
        if (Invoke-D4File -Boundary $fsBoundary -Operation 'IsLeaf' -Arguments @($ConfigPath)) {
            $existing = [string](Invoke-D4File -Boundary $fsBoundary -Operation 'ReadText' -Arguments @($ConfigPath))
            if ($existing.Trim() -ne $cfgJson.Trim()) {
                throw ("T0 refused: the experiment config already exists with DIFFERENT content (no clobber): " + $ConfigPath)
            }
            if ($recoveryMode) {
            Write-Output ("T0R complete (resume): account '" + $plan['account_name'] + "' (SID " +
                          $bootSid + ") and share task '" + $contract['ShareTaskName'] +
                          "' already registered; the SAME config was re-emitted to " + $ConfigPath)
            } else {
            Write-Output ("T0 complete (resume): account '" + $plan['account_name'] + "' (SID " +
                          $bootSid + ") and share task '" + $contract['ShareTaskName'] +
                          "' already registered; the SAME config was re-emitted to " + $ConfigPath)
            }
            exit 0
        }
        Invoke-D4File -Boundary $fsBoundary -Operation 'WriteText' -Arguments @(
            $ConfigPath, $cfgJson)
        if (-not $recoveryMode) {
        Write-Output ("T0 complete: account '" + $plan['account_name'] + "' created (SID " +
                      $bootSid + "), share task '" + $contract['ShareTaskName'] +
                      "' registered (password-managed; the credential was NOT persisted); " +
                      "experiment config emitted to " + $ConfigPath)
        } else {
        Write-Output ("T0R complete: account '" + $plan['account_name'] + "' (SID " +
                      $bootSid + ") recovered (password reset + enable; the credential was NOT persisted); " +
                      "share task '" + $contract['ShareTaskName'] +
                      "' registered; experiment config emitted to " + $ConfigPath)
        }
        exit 0
        } catch {
            # §118.2: after a recovery the account is ENABLED and the task
            # REGISTERED - ANY later config generation/validation/emission
            # failure runs the SAME bounded close before the process exits.
            if ($recoveryMode) {
                $crCfg = Invoke-D4RecoveryClose -Contract $contract -RunId $ApprovedRunId `
                    -AccountName $plan['account_name'] -TaskName $contract['ShareTaskName'] `
                    -FixedSid $RecoveryFixedSid -RecoveryStatePath $recoveryStatePath `
                    -PackageHash $bootstrapIdentity -PlanIdentity $planIdentity `
                    -AccountRunner $AccountRunner -FileBoundary $fsBoundary `
                    -StoreProbe $recoveryStoreProbe -AclProbe $BootstrapAclProbe
                $cfgCloseNote = if ($crCfg.ok) { 'recovery close: disabled by the fixed SID (post-verified)' }
                                else { 'recovery close FAILED (no disabled terminal): ' + [string]$crCfg.reason }
                throw ("T0R config/emission failed after recovery; " + $cfgCloseNote + " :: " + $_.Exception.Message)
            }
            throw $_
        }
    }
    'S0' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'S0' -FileBoundary $fsBoundary)
        # R62-B: the inventory itself is a PURE READ (zero experiment
        # writes). The ONLY declared write is the evidence record, and it is
        # written ONLY when the verdict is FREE - an occupied/unconfirmed
        # namespace never receives a record (nothing is created inside a
        # namespace this run does not own).
        # §93: for a password-managed run, THIS run's own share task was
        # already registered by T0 - the bootstrap record (run-id bound, SID
        # + Password proof) exempts it from occupancy; anything else stays a
        # foreign occupant ($ownShare computed once above).
        $free = Test-D4NamespaceFree -Inventory $inventory -Contract $contract `
            -SchtasksRunner $SchtasksRunner -NetStatRunner $NetStatRunner -FileBoundary $fsBoundary `
            -OwnShareTask $ownShare
        if ($free['Verdict'] -ne 'free') {
            throw ("S0 inventory verdict=" + $free['Verdict'] + "; refusing (never clear or overwrite; zero records written): " +
                   ($free['Details'] -join '; '))
        }
        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
            -Stage 'S0' -Verdict 'pass' -Detail 'tri-state inventory free; evidence record is the declared write' `
            -FileBoundary $fsBoundary)
        Write-Output ("S0 complete for run " + $ApprovedRunId + ": namespace free (task/port/root/runtime checked)")
        exit 0
    }
    'S1' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'S1' -FileBoundary $fsBoundary)
        # Write-ORDER contract (R62-B): the namespace re-check is a PURE
        # read and refuses BEFORE any write; the intent ledger is persisted
        # BEFORE any object; then every object is created per-object with an
        # existence re-check (no Force takeover) and an ownership record.
        # §93: the re-check carries the same run-owned task exemption as S0.
        $free = Test-D4NamespaceFree -Inventory $inventory -Contract $contract `
            -SchtasksRunner $SchtasksRunner -NetStatRunner $NetStatRunner -FileBoundary $fsBoundary `
            -OwnShareTask $ownShare
        if ($free['Verdict'] -ne 'free') {
            throw ("S1 refused: namespace is no longer free (zero writes performed): " + ($free['Details'] -join '; '))
        }
        if ([string]$config.identity_source -ne 'collected') {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'S1' -Verdict 'blocked' -Detail 'identity_source is pending-collection; S1 requires the collected operator/share identity (D4-X input)' `
                -FileBoundary $fsBoundary)
            throw 'S1 blocked: collected identity is required before any experiment object is created'
        }
        $ledger = New-D4IntentLedger -Inventory $inventory -LedgerPath $ledgerPath -FileBoundary $fsBoundary
        try {
            $ownEvidence = @{}
            foreach ($f in @($inventory['Files'])) {
                if ($f.Kind -in 'intent-ledger', 'stage-state') { $ownEvidence[[string]$f.Path] = $true }
            }
            foreach ($d in $inventory['Dirs']) {
                if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($d)) {
                    # S0's declared evidence write physically creates the
                    # evidence dir on the REAL filesystem (the fake model
                    # never materializes parent dirs, so offline tests cannot
                    # see this). The same planned-ownership carve-out as
                    # Test-D4NamespaceFree applies: the dir may pre-exist ONLY
                    # while it holds nothing but THIS run's own state records;
                    # anything else is foreign takeover and refuses.
                    $okPreExisting = $false
                    if ($d -eq (Join-Path $resolvedRoot $contract['StateDirRel'])) {
                        $okPreExisting = $true
                        foreach ($i in @(Invoke-D4File -Boundary $fsBoundary -Operation 'List' -Arguments @($d))) {
                            if (-not $ownEvidence.ContainsKey([string]$i)) { $okPreExisting = $false }
                        }
                    }
                    if (-not $okPreExisting) {
                        throw ("S1 refused: planned directory already exists (no takeover): " + $d)
                    }
                    Write-Log ("planned dir already holds THIS run's S0 evidence records: " + $d)
                    continue
                }
                Invoke-D4File -Boundary $fsBoundary -Operation 'MakeDir' -Arguments @($d)
                $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path $d -Status 'created' -FileBoundary $fsBoundary
            }
            foreach ($f in @($inventory['Files']) | Where-Object { $_.Kind -in 'owned-copy', 'runtime-copy' }) {
                if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($f.Path)) {
                    throw ("S1 refused: planned file already exists (no takeover): " + $f.Path)
                }
                # R78-A: owned/runtime copies are BYTE-EXACT copies - a
                # text round-trip would corrupt exe/DLL bytes and alter
                # source encoding. The copy verifies against the inventoried
                # sha256 and records the ACTUAL hash for the cleanup gate.
                Invoke-D4File -Boundary $fsBoundary -Operation 'CopyBytes' -Arguments @($f.Source, $f.Path)
                $actual = Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($f.Path)
                if ($actual -ne $f.Sha256) {
                    throw ("S1 copy hash mismatch for " + $f.Path)
                }
                $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path $f.Path -Status 'created' -Sha256 $actual -FileBoundary $fsBoundary
                Write-Log ("copied+verified " + (Split-Path $f.Path -Leaf) + " (" + $actual.Substring(0, 12) + "...)")
            }
            # Every generated file records its ACTUAL hash at creation (the
            # cleanup's hash-verified removal basis) and refuses to take over
            # any pre-existing object.
            $deployModePath = Join-Path $resolvedRoot $contract['DeployModeRel']
            if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($deployModePath)) {
                throw ("S1 refused: deploy-mode fact already exists (no takeover): " + $deployModePath)
            }
            Invoke-D4File -Boundary $fsBoundary -Operation 'WriteText' -Arguments @($deployModePath, '{"mode": "projection"}')
            $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path $deployModePath -Status 'created' `
                -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($deployModePath)) -FileBoundary $fsBoundary
            $projectionPath = Join-Path $resolvedRoot $contract['ProjectionRel']
            if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($projectionPath)) {
                throw ("S1 refused: projection fact already exists (no takeover): " + $projectionPath)
            }
            $secretBytes = [byte[]]::new(32)
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
        $secret = -join ($secretBytes | ForEach-Object { '{0:x2}' -f $_ })
            $projection = @{ schema_version = 1; config_generation = 1;
                             data = @(@{ key_id = 'exp-friend'; name = 'exp-friend';
                                         secret_hash = $secret; secret_version = 1; enabled = $true }) }
            Invoke-D4File -Boundary $fsBoundary -Operation 'WriteText' -Arguments @($projectionPath, ($projection | ConvertTo-Json -Depth 4))
            $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path $projectionPath -Status 'created' `
                -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($projectionPath)) -FileBoundary $fsBoundary
            # The validated experiment config is the guard's launch-pinning
            # consumer input (written BEFORE the capture wrapper references it).
            $expConfigPath = Join-Path $resolvedRoot 'experiment-config.json'
            if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($expConfigPath)) {
                throw ("S1 refused: experiment config already exists (no takeover): " + $expConfigPath)
            }
            Invoke-D4File -Boundary $fsBoundary -Operation 'WriteText' -Arguments @($expConfigPath, ($config | ConvertTo-Json -Depth 6))
            $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path $expConfigPath -Status 'created' `
                -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($expConfigPath)) -FileBoundary $fsBoundary
            # The broker side reuses the REVIEWED install path with the fixed
            # experiment namespace; the image/runtime copies above satisfy
            # its prerequisites. NOTHING is started. The install goes through
            # the assembly hook (real mode: the reviewed Install-Broker;
            # harness: the injected recording hook - never the real path).
            $brokerCfg = New-BrokerConfig -RunRoot $resolvedRoot -SourceRoot $sourceRoot `
                -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
                -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
                -OperatorAccount ([string]$config.operator_account) -InstanceOwnerSid ([string]$config.share_sid) `
                -BrokerPipeSddl ([string]$config.broker_pipe_sddl) -PythonHome ([string]$config.python_home) `
                -SharePort $contract['SharePort'] -ShareImage $contract['ShareImage'] `
                -ExpectedOwnerAccount ([string]$config.share_account) `
                -TaskTrigger $contract['TaskTrigger']
            if ($BrokerInstaller) {
                # R78-C: the injected hook also receives the file boundary it
                # writes the install's OWN manifest through (the real install
                # writes it on the real filesystem; the manifest is the
                # record the import below consumes).
                & $BrokerInstaller $brokerCfg $SchtasksRunner $icaclsRunner $fsBoundary
            } else {
                Install-Broker -Cfg $brokerCfg -SchtasksRunner $SchtasksRunner -IcaclsRunner $icaclsRunner
            }
            # R78-C: the install's OWN manifest decides what THIS run
            # created. Only entries the install classified 'created' with a
            # non-empty sha256 are imported into the intent ledger WITH
            # their actual hash (the cleanup deletion gate then works);
            # 'shared'/'planned' pre-existing artifacts stay out of this
            # run's owned set (the install's own manifest owns their
            # recovery). install-state.json itself is frozen AFTER the
            # completed install (it is this run's record once Install
            # returned success).
            $installManifestPath = Join-Path $resolvedRoot 'broker\install-state.json'
            $ledger = Import-D4InstallManifestOwnership -ManifestPath $installManifestPath `
                -ResolvedRoot $resolvedRoot -Ledger $ledger -LedgerPath $ledgerPath -FileBoundary $fsBoundary
            # install-state.json itself: frozen after the completed install.
            $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                -Path (Join-Path $resolvedRoot 'broker\install-state.json') -Status 'created' `
                -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @(
                    (Join-Path $resolvedRoot 'broker\install-state.json'))) `
                -FileBoundary $fsBoundary
            # share task: needs the COLLECTED share identity + established
            # logon method; the capture script uses ABSOLUTE paths and pins
            # the work directory (task default cwd must not be assumed).
            if ([string]$config.logon_method -in 'task-logon', 'password-managed') {
                $shareArgs = New-D4ShareLaunchArgs -Contract $contract `
                    -InstanceKeyPath (Join-Path $resolvedRoot 'instance-channel-key') `
                    -RegistrationPath (Join-Path $resolvedRoot $contract['RegistrationRel']) `
                    -InstancePipeSddl ([string]$config.instance_pipe_sddl)
                $capture = New-D4ShareCaptureScriptContent -Contract $contract -ShareArgs $shareArgs `
                    -PythonHome ([string]$config.python_home)
                $capturePath = Join-Path $resolvedRoot 'run-live.cmd'
                if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($capturePath)) {
                    throw ("S1 refused: capture wrapper already exists (no takeover): " + $capturePath)
                }
                Invoke-D4File -Boundary $fsBoundary -Operation 'WriteText' -Arguments @($capturePath, $capture)
                $shareLogonType = if ([string]$config.logon_method -eq 'password-managed') { 'Password' } else { 'S4U' }
                $shareDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config $config `
                    -CommandPath $capturePath -LogonType $shareLogonType
                $shareDefPath = Join-Path $resolvedRoot 'run-live-task-definition.xml'
                if (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($shareDefPath)) {
                    throw ("S1 refused: share task definition already exists (no takeover): " + $shareDefPath)
                }
                Invoke-D4File -Boundary $fsBoundary -Operation 'WriteXml' -Arguments @($shareDefPath, $shareDef)
                if ([string]$config.logon_method -eq 'task-logon') {
                    $capResult = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
                        'schtasks', '/Create', '/TN', $contract['ShareTaskName'], '/XML', $shareDefPath)
                    if ($capResult.ExitCode -ne 0) {
                        # The schtasks failure text is the first actionable fact
                        # for the operator (e.g. client-side encoding rejection
                        # vs server-side access denial) - surface it with the
                        # exit code instead of swallowing it.
                        throw ("experiment share task registration failed (exit " + $capResult.ExitCode + "): " + ($capResult.Output -join ' '))
                    }
                } else {
                    # password-managed: T0 registered the task with an ephemeral
                    # credential in ITS process; S1 NEVER re-registers (no
                    # credential exists here). The FULL definition is verified
                    # instead - SID + Password logon + command - and a mismatch
                    # or absence aborts (only T0 may create it).
                    $shareTaskSpec = @{ Name = $contract['ShareTaskName'];
                                        IdentitySid = [string]$config.share_sid;
                                        LogonType = 'Password'; Command = $capturePath }
                    $ident = Confirm-D4TaskIdentity -Task $shareTaskSpec -Trigger 'ONDEMAND' `
                        -SchtasksRunner $SchtasksRunner -LogonType 'Password'
                    if ($ident.State -ne 'owned') {
                        throw ("S1 password-managed: share task identity NOT confirmed (state=" + $ident.State +
                               " reason=" + $ident.Reason + "); only T0 registers it and no credential is available here - abort")
                    }
                }
                foreach ($rel in @('run-live.cmd', 'run-live-task-definition.xml')) {
                    # R67-C evidence basis: EVERY created file records its
                    # ACTUAL hash at creation - the cleanup deletion gate
                    # deletes only fingerprint-verified objects.
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path (Join-Path $resolvedRoot $rel) -Status 'created' `
                        -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @((Join-Path $resolvedRoot $rel))) `
                        -FileBoundary $fsBoundary
                }
                $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                    -Path ('task:' + $contract['ShareTaskName']) -Status 'created' -FileBoundary $fsBoundary
            } elseif ([string]$config.logon_method -eq 'unestablished') {
                Write-Log 'S1: share task NOT registered (um_share logon availability unestablished; per contract it must not be registered or started)'
            }
            $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                -Path ('task:' + $contract['BrokerTaskName']) -Status 'created' -FileBoundary $fsBoundary
            # R67-B (§60.5-S1 order): the experiment DACL matrix is prepared
            # BEFORE the limit rules (permissions/limits ready -> start).
            # Ownership is ledger-recorded before each change; the pre-change
            # ACL is backed up into the evidence dir; a failed grant is
            # restored (bounded recovery) with the state table recording the
            # outcome (applied/none/failed). um_share is granted ONLY on the
            # run\live registration spot (per-object identity matrix).
            $acl = Invoke-D4PrepareExperimentAcl -Root $resolvedRoot `
                -RunDir (Join-Path $resolvedRoot $contract['GuardRunRel']) `
                -Cfg $config -IcaclsRunner $icaclsRunner -Ledger $ledger `
                -LedgerPath $ledgerPath -FileBoundary $fsBoundary `
                -EvidenceDir (Join-Path $resolvedRoot $contract['StateDirRel'])
            $ledger = $acl['Ledger']
            # E1 (W2): the share task launches share_guard.py AS the
            # experiment share account; on this host the python stdlib lives
            # in the collected python_home and the account needs the SAME
            # minimal read+execute grant production gives um_share - without
            # it the share task dies instantly ("No module named
            # 'encodings'"). Ledger-gated, verified, reversed in cleanup.
            if ([string]$config.logon_method -in 'task-logon', 'password-managed') {
                $pyHome = Invoke-D4PrepareSharePythonHomeAccess `
                    -PythonHome ([string]$config.python_home) `
                    -ShareSid ([string]$config.share_sid) `
                    -IcaclsRunner $icaclsRunner -Ledger $ledger `
                    -LedgerPath $ledgerPath -FileBoundary $fsBoundary
                $ledger = $pyHome['Ledger']
            }
            # R67-B: the experiment's OWN limit rules are created here (the
            # fixed isolation set, ledger-recorded, STRONG-verified after
            # each create). Creation is a D4-X authorized action - this runs
            # only under the approved real mode; a same-id FOREIGN rule
            # refuses takeover. Rules are created but NEVER started - they
            # are passive filters; the E1 gate re-verifies them strongly.
            $limits = Invoke-D4PrepareLimits -Inventory $inventory -RulesRunner $RulesRunner `
                -Contract $contract -Ledger $ledger -LedgerPath $ledgerPath -FileBoundary $fsBoundary
            $ledger = $limits['Ledger']
            Write-Log 'S1 prepare complete: experiment root populated, DACL matrix + limit rules applied, ONDEMAND tasks registered, NOTHING started'
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'S1' -Verdict 'pass' -Detail 'prepared; tasks not started' -FileBoundary $fsBoundary)
            exit 0
        } catch {
            # Bounded recovery: keep the ledger, list every created-but-
            # unresolved object, record the failure - never claim success.
            # (A ledger write failure itself leaves $ledger null -> empty list.)
            # R67-B: the ACL recovery runs FIRST (best-effort restore; a
            # failed restore keeps the backup + state=failed for review -
            # the state table, not a silent pass, is the record).
            try {
                # R69-C: the S1 abort path rolls back APPLIED dirs too - the
                # experiment is abandoned; every applied matrix rolls back.
                [void](Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $icaclsRunner `
                    -FileBoundary $fsBoundary `
                    -EvidenceDir (Join-Path $resolvedRoot $contract['StateDirRel']) `
                    -ExperimentRoot $resolvedRoot -RollbackApplied)
            } catch {
                Write-Log ("WARNING: acl recovery attempt failed: " + $_.Exception.Message)
            }
            # R78-C: a HALF-completed install must be recorded on the SAME
            # run's evidence - not discovered only after Install returned.
            # Every manifest entry the install already classified 'created'
            # with a real hash (and the manifest itself) is imported into
            # this run's intent ledger, so the same-run cleanup can finish
            # exactly these objects by identity; anything the install never
            # recorded stays 'planned' and the cleanup refuses it for
            # operator review. Best-effort: a failed import keeps the
            # primary failure's record (the honest outcome stays a refusal).
            try {
                $abortManifestPath = Join-Path $resolvedRoot 'broker\install-state.json'
                if ($null -ne $ledger -and
                        (Invoke-D4File -Boundary $fsBoundary -Operation 'IsLeaf' -Arguments @($abortManifestPath))) {
                    $ledger = Import-D4InstallManifestOwnership -ManifestPath $abortManifestPath `
                        -ResolvedRoot $resolvedRoot -Ledger $ledger -LedgerPath $ledgerPath -FileBoundary $fsBoundary
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $abortManifestPath -Status 'created' `
                        -Sha256 (Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($abortManifestPath)) `
                        -FileBoundary $fsBoundary
                }
            } catch {
                Write-Log ("WARNING: install manifest import at the S1 abort failed: " + $_.Exception.Message)
            }
            $unresolved = if ($null -ne $ledger) { @(Get-D4LedgerUnresolved -Ledger $ledger) } else { @() }
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'S1' -Verdict 'fail' -Detail ('failure: ' + $_.Exception.Message +
                    '; unresolved objects: ' + (($unresolved) -join '; ')) -FileBoundary $fsBoundary)
            throw
        }
    }
    'E1' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'E1' -FileBoundary $fsBoundary)
        # §60.5 E1 ORDER (locked): limits ready -> placeholder-SDDL negative
        # -> correct-DACL starts -> identity/listener verification -> REAL
        # protocol matrix. Every gate refuses BEFORE any start; a failure
        # never advances.
        # Gate 0: the share probe task must be registered (the 9100 listener
        # is the share child; an unestablished um_share logon cannot start).
        # password-managed counts as ESTABLISHED: T0 registered the task with
        # the collected SID (the task itself is the collected fact).
        if ([string]$config.logon_method -notin 'task-logon', 'password-managed') {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'blocked' -Detail 'the share task is not registered (share logon availability unestablished); the 9100 listener cannot be verified' `
                -FileBoundary $fsBoundary)
            throw 'E1 blocked: the share probe task requires an established share logon method (collected fact or registered password-managed task)'
        }
        # Gate 1: collected identity - placeholder values can never pass.
        if ([string]$config.identity_source -ne 'collected') {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'blocked' -Detail 'collected identity required (D4-X input); placeholder values can never pass a real branch' `
                -FileBoundary $fsBoundary)
            throw 'E1 blocked: identity collection is a D4-X input'
        }
        # Gate 2 (R67-A): experiment limits MUST be in place AND match their
        # EXACT specifications before any start. The rules collector is
        # REQUIRED (a missing collector is an unconfirmed state, never a
        # pass); a same-key rule with a different action/layer/app-id/scope
        # is a MISMATCH and refuses with zero starts - existence alone can
        # never prove isolation.
        if (-not $RulesRunner) {
            throw 'E1 refused: no limit-rule collector supplied (limits state UNCONFIRMED; zero starts)'
        }
        $limits = Test-D4LimitsReady -Inventory $inventory -RulesRunner $RulesRunner -Contract $contract
        if (-not $limits['Ready']) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail ('limits not ready: ' + ($limits['Details'] -join '; ')) -FileBoundary $fsBoundary)
            throw ("E1 refused: experiment limits not ready (ZERO starts): " + ($limits['Details'] -join '; '))
        }
        # Gate 3: the probe runner is REQUIRED for the negative-first probe
        # and the request matrix (never a fallback).
        if (-not $ProbeRunner) {
            throw 'E1 refused: no probe collector supplied (the DACL negative and matrix cannot run; zero starts)'
        }
        # Negative FIRST: a PLACEHOLDER SDDL must fail the pipe creation
        # (the conversion refuses on purpose - a success proves the DACL
        # assembly path is broken).
        $neg = Invoke-CommandRunner -Runner $ProbeRunner -ArgumentList @('probe', 'pipe-probe',
            '--name', ($contract['InstancePipePrefix'] + 'negative'),
            '--sddl', 'D:P(A;;FA;;;S-1-5-18-exp)')
        if ($neg.ExitCode -eq 0) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail 'placeholder SDDL created a pipe' -FileBoundary $fsBoundary)
            throw ('E1 negative FAILED: a placeholder SDDL created a pipe - the DACL assembly path is broken (zero starts)')
        }
        # §95.5: pre-start FULL identity verification - EVERY experiment task
        # is verified against its expected definition (exact command, single
        # Exec, single Principal, exact SID, LogonType + implied RunLevel, no
        # automatic trigger) IMMEDIATELY before /Run; a mismatched or absent
        # task NEVER starts (zero starts).
        foreach ($startTask in @($inventory['Tasks'])) {
            $startIdent = Confirm-D4TaskIdentity -Task $startTask -Trigger ([string]$startTask['Trigger']) `
                -SchtasksRunner $SchtasksRunner -LogonType ([string]$startTask['LogonType'])
            if ($startIdent.State -ne 'owned') {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E1' -Verdict 'fail' -Detail ('pre-start identity not confirmed for ' + $startTask.Name +
                    ' (state=' + $startIdent.State + ' reason=' + $startIdent.Reason + ')') -FileBoundary $fsBoundary)
                throw ("E1 refused: task identity NOT confirmed before start (" + $startTask.Name +
                       ": " + $startIdent.State + " " + $startIdent.Reason + "); zero starts")
            }
        }
        # Start the share probe task and the broker task (ONDEMAND /Run).
        $startShare = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
            'schtasks', '/Run', '/TN', $contract['ShareTaskName'])
        $startBroker = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
            'schtasks', '/Run', '/TN', $contract['BrokerTaskName'])
        if ($startShare.ExitCode -ne 0 -or $startBroker.ExitCode -ne 0) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail 'task start failed' -FileBoundary $fsBoundary)
            throw 'E1 failed: experiment task start refused/failed'
        }
        # Broker management pipe readiness: the broker owns ONE pipe instance
        # per connection cycle, so the pipe name vanishes for ~1.5ms between
        # cycles and may not exist yet right after /Run. Wait for the
        # management pipe to exist (bounded) so the matrix runs against a
        # live broker; the reconnect race itself is covered by the client's
        # bounded open retry (share/broker_client.py, W2 run20 root cause).
        $brokerReady = $false
        $brokerDeadline = (Get-Date).AddSeconds(25)
        while ((Get-Date) -lt $brokerDeadline) {
            $pipeRun = if ($PipeEvidenceRunner) {
                & $PipeEvidenceRunner
            } else { @( [System.IO.Directory]::GetFiles('\\.\pipe\') ) }
            if (@($pipeRun | Where-Object { $_ -match 'um-share-broker-exp' }).Count -gt 0) {
                $brokerReady = $true; break
            }
            Start-Sleep -Milliseconds 400
        }
        if (-not $brokerReady) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail 'broker management pipe never appeared after start' -FileBoundary $fsBoundary)
            throw 'E1 failed: broker management pipe not ready (zero matrix attempts)'
        }
        # Listener + identity verification (bounded wait; netstat collector
        # REQUIRED). The process evidence uses the experiment-wide collector
        # (image path OR command line) - the ShareImage-name filter variant
        # embeds a WQL filter whose quotes are destroyed by the runner's
        # -Command pass-through (empirically: 'invalid query' on this host),
        # so a name-only probe can never confirm the share process.
        if (-not $NetStatRunner) {
            throw 'E1 refused: no netstat collector supplied (listener state UNCONFIRMED)'
        }
        $deadline = (Get-Date).AddSeconds(30)
        $listening = $false
        while ((Get-Date) -lt $deadline) {
            $net = Invoke-CommandRunner -Runner $NetStatRunner -ArgumentList @('netstat', '-ano', '-p', 'TCP')
            if ((@($net.Output | Where-Object { $_ -match ':\s*9100\s' -and $_ -match 'LISTENING' })).Count -gt 0) {
                $listening = $true; break
            }
            Start-Sleep -Milliseconds 400
        }
        $procEvi = Get-D4ExperimentProcessEvidence `
            -ExperimentRoot $resolvedRoot -Runner $ProcessEvidenceRunner
        if (-not $listening -or $procEvi.Unconfirmed -or $procEvi.Pids.Count -eq 0) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail 'listener or process identity unconfirmed' -FileBoundary $fsBoundary)
            throw 'E1 failed: share probe listener/identity unconfirmed (stop later stages, keep evidence)'
        }
        # Request matrix via the REAL protocol probe (OS edges injected). The
        # operator-provable E1 verdicts: management status ok + the two
        # management negatives + the 9100 listener refusing a wrong key (the
        # correct-key 200 check needs the test key plaintext and is E3's
        # evidence, where the store owns the key).
        $matrixSpec = @{ port = $contract['SharePort'];
                         management_pipe = $contract['BrokerPipeName'];
                         instance_prefix = $contract['InstancePipePrefix'];
                         control_key = (Join-Path $resolvedRoot 'broker\broker-control-key');
                         registration = (Join-Path $resolvedRoot $contract['RegistrationRel']) } |
            ConvertTo-Json -Compress
        # The probe spec is a JSON string whose DOUBLE QUOTES are destroyed by
        # the PS 5.1 -> native argv boundary (an embedded '"' is swallowed, so
        # json.loads sees '{port:9100,...}' and fails). Transport the spec as
        # BASE64 with a 'b64:' marker - no quote ever crosses the boundary;
        # the probe decodes it before parsing (W2 run17).
        $matrix = Invoke-CommandRunner -Runner $ProbeRunner -ArgumentList @('probe', 'request-matrix',
            '--spec', ('b64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($matrixSpec))))
        $matrixText = (@($matrix.Output) -join "`n")
        if ($matrix.ExitCode -ne 0) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E1' -Verdict 'fail' -Detail ('request matrix failed: ' + $matrixText) -FileBoundary $fsBoundary)
            throw 'E1 failed: request matrix did not complete'
        }
        foreach ($expected in @('mgmt-status-ok', 'mgmt-wrong-hmac-refused',
                                'mgmt-replay-refused', 'http-wrong-key-refused')) {
            if ($matrixText -notmatch [regex]::Escape($expected)) {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E1' -Verdict 'fail' -Detail ('matrix missing verdict: ' + $expected) -FileBoundary $fsBoundary)
                throw ("E1 failed: request matrix lacks verdict " + $expected)
            }
        }
        # R83 (W2 run21): E1's runtime wrote live status/registration into
        # the ledger-planned dynamic artifacts - offline their final state is
        # not provable, so the STARTING stage marks EXACTLY these three
        # 'retained' (legal terminal, per-object reason, never a deletion
        # right). The bounded list matches E3's two-artifact pattern: only
        # the artifacts this stage's runtime actually writes.
        $ledger = Save-D4DynamicRetention -LedgerPath $ledgerPath -PackageHash $packageHash `
            -RunId $ApprovedRunId -ExperimentRoot $resolvedRoot `
            -RelativePaths @('broker\broker-status.json',
                             ($contract['GuardRunRel'] + '\guard-status.json'),
                             $contract['RegistrationRel']) `
            -Retention 'E1 runtime wrote live status/registration telemetry; per-run TRANSIENT artifacts retained (R83), never granted deletion' `
            -FileBoundary $fsBoundary
        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
            -Stage 'E1' -Verdict 'pass' -Detail 'negative-first DACL + started + identity + matrix verified' `
            -FileBoundary $fsBoundary)
        Write-Output ("E1 complete for run " + $ApprovedRunId)
        exit 0
    }
    'E2' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'E2' -FileBoundary $fsBoundary)
        # §60.5 E2: single-connection silent / drip / blocking-read classes,
        # each bounded (deadline + max attempts), measured against the REAL
        # broker management pipe; a post-timeout legal request must still
        # complete. The probe collector is REQUIRED (never a fallback).
        if (-not $ProbeRunner) {
            throw 'E2 refused: no probe collector supplied (the bounded cancellation matrix cannot run)'
        }
        $stallSpec = @{ pipe = $contract['BrokerPipeName'];
                        status_dir = (Join-Path $resolvedRoot 'broker');
                        control_key = (Join-Path $resolvedRoot 'broker\broker-control-key');
                        max_per_class = 3;
                        deadline_seconds = 5 } | ConvertTo-Json -Compress
        $result = Invoke-CommandRunner -Runner $ProbeRunner -ArgumentList @('probe', 'stall-matrix',
            '--spec', ('b64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stallSpec))))
        $text = (@($result.Output) -join "`n")
        if ($result.ExitCode -ne 0) {
            [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                -Stage 'E2' -Verdict 'fail' -Detail ('stall matrix failed: ' + $text) -FileBoundary $fsBoundary)
            throw 'E2 failed: bounded cancellation not confirmed'
        }
        foreach ($expected in @('silent-cancelled', 'drip-cancelled', 'blocking-cancelled',
                                'post-timeout-request-ok')) {
            if ($text -notmatch [regex]::Escape($expected)) {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E2' -Verdict 'fail' -Detail ('missing verdict: ' + $expected) -FileBoundary $fsBoundary)
                throw ("E2 failed: cancellation evidence lacks " + $expected)
            }
        }
        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
            -Stage 'E2' -Verdict 'pass' -Detail 'bounded cancellation verified (max 3/class, deadline honored)' `
            -FileBoundary $fsBoundary)
        Write-Output ("E2 complete for run " + $ApprovedRunId)
        exit 0
    }
    'E3' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'E3' -FileBoundary $fsBoundary)
        # §60.5 E3: publish through the REAL store + REAL management logic,
        # ACK bound to generation/fingerprint/id, 9100 correct/incorrect key,
        # revocation refused for the old key. The probe collector is
        # REQUIRED. The isolated test store is created by the probe (the
        # real ShareStore) and is recorded into the intent ledger here.
        if (-not $ProbeRunner) {
            throw 'E3 refused: no probe collector supplied (the projection lifecycle cannot run)'
        }
        $lifecycleSpec = @{ store = (Join-Path $resolvedRoot 'share-store.db');
                            projection = (Join-Path $resolvedRoot $contract['ProjectionRel']);
                            management_pipe = $contract['BrokerPipeName'];
                            port = $contract['SharePort'];
                            control_key = (Join-Path $resolvedRoot 'broker\broker-control-key') } |
            ConvertTo-Json -Compress
        # R83: the publish chain REWRITES the projection and creates/mutates
        # the isolated store - offline their final state is NOT provable the
        # way the deletion gate requires, so the WRITING stage marks them
        # 'retained' (legal terminal, per-object reason, NEVER a deletion
        # right); a failure AFTER the writes still records the retention so
        # the same run's cleanup closes over a clear per-object record.
        try {
            $result = Invoke-CommandRunner -Runner $ProbeRunner -ArgumentList @('probe', 'projection-lifecycle',
                '--spec', ('b64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($lifecycleSpec))))
            $text = (@($result.Output) -join "`n")
            if ($result.ExitCode -ne 0) {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E3' -Verdict 'fail' -Detail ('lifecycle failed: ' + $text) -FileBoundary $fsBoundary)
                throw 'E3 failed: projection/rotation closure not verified'
            }
            foreach ($expected in @('ack-generation-matched', 'http-key-ok', 'wrong-key-401',
                                    'revoked-key-refused')) {
                if ($text -notmatch [regex]::Escape($expected)) {
                    [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                        -Stage 'E3' -Verdict 'fail' -Detail ('missing verdict: ' + $expected) -FileBoundary $fsBoundary)
                    throw ("E3 failed: closure evidence lacks verdict " + $expected)
                }
            }
            # R83 retention (BOUNDED: exactly the two artifacts this stage writes).
            $ledger = Save-D4DynamicRetention -LedgerPath $ledgerPath -PackageHash $packageHash `
                -RunId $ApprovedRunId -ExperimentRoot $resolvedRoot `
                -RelativePaths @($contract['ProjectionRel'], 'share-store.db') `
                -Retention 'E3 publish chain rewrote the projection/store; offline final-state evidence not provable; retained (never granted deletion)' `
                -FileBoundary $fsBoundary
        } catch {
            # R83: the probe may have mutated the projection/store before
            # failing - the retention is recorded so the interrupted record
            # never deadlocks the same run's cleanup.
            try {
                $ledger = Save-D4DynamicRetention -LedgerPath $ledgerPath -PackageHash $packageHash `
                    -RunId $ApprovedRunId -ExperimentRoot $resolvedRoot `
                    -RelativePaths @($contract['ProjectionRel'], 'share-store.db') `
                    -Retention 'E3 aborted after the publications landed; offline final-state evidence not provable; retained (never granted deletion)' `
                    -FileBoundary $fsBoundary
            } catch {
                Write-Log ("WARNING: dynamic retention record at the E3 abort failed: " + $_.Exception.Message)
            }
            try {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E3' -Verdict 'fail' -Detail ('E3 aborted: ' + $_.Exception.Message) -FileBoundary $fsBoundary)
            } catch {
                # an inline fail path already recorded the verdict - keep it
                if ("$($_.Exception.Message)" -notlike '*already recorded*') {
                    Write-Log ("WARNING: E3 abort state record failed: " + $_.Exception.Message)
                }
            }
            throw
        }
        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
            -Stage 'E3' -Verdict 'pass' -Detail 'real store publish/ACK/revocation verified' -FileBoundary $fsBoundary)
        Write-Output ("E3 complete for run " + $ApprovedRunId)
        exit 0
    }
    'E4' {
        [void](Test-D4StageAdvance -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId -Stage 'E4' -FileBoundary $fsBoundary)
        # §60.5 E4: fault injection ONLY at the audited boundaries of THIS
        # experiment's own objects. The PS-domain boundaries (ACL change /
        # backup cleanup / task register) run the REVIEWED install/recovery
        # code with the failure injected at exactly one point; the
        # projection-write boundary is the probe's fault-injection against
        # the real store + manager + writer. Every scenario must show
        # refusal + a recorded recovery - a fake never-success cannot cover
        # them (each scenario's verdicts are asserted individually).
        if (-not $ProbeRunner) {
            throw 'E4 refused: no probe collector supplied (the projection-write boundary cannot run)'
        }
        # Caller-scoped scenario root: the REAL run injects faults on the
        # experiment install itself (reentry over the S1 install); the test
        # assembly MUST inject an isolated temp root so no offline run ever
        # touches the real experiment root. NOTE: the local variable is NOT
        # named like the -ScenarioRoot parameter (PowerShell identifiers are
        # case-insensitive - assigning the local would clobber the bound
        # parameter value and the harness override would become a
        # no-op self-assignment).
        $faultRoot = $resolvedRoot
        $brokerCfg = New-BrokerConfig -RunRoot $faultRoot -SourceRoot $sourceRoot `
            -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
            -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
            -OperatorAccount ([string]$config.operator_account) -InstanceOwnerSid ([string]$config.share_sid) `
            -BrokerPipeSddl ([string]$config.broker_pipe_sddl) -PythonHome ([string]$config.python_home) `
            -SharePort $contract['SharePort'] -ShareImage $contract['ShareImage'] `
            -ExpectedOwnerAccount ([string]$config.share_account) `
            -TaskTrigger $contract['TaskTrigger']
        if ($TestHarness) {
            if (-not $ScenarioRoot -or -not $IcaclsRunner -or -not $FailingIcaclsRunner -or
                    -not $FailingSchtasksRunner) {
                throw 'test harness E4 requires -ScenarioRoot and the injected icacls/schtasks fault runners'
            }
            $faultRoot = $ScenarioRoot
            $brokerCfg = New-BrokerConfig -RunRoot $faultRoot -SourceRoot $sourceRoot `
                -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
                -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
                -OperatorAccount ([string]$config.operator_account) -InstanceOwnerSid ([string]$config.share_sid) `
                -BrokerPipeSddl ([string]$config.broker_pipe_sddl) -PythonHome ([string]$config.python_home) `
                -SharePort $contract['SharePort'] -ShareImage $contract['ShareImage'] `
                -ExpectedOwnerAccount ([string]$config.share_account) `
                -TaskTrigger $contract['TaskTrigger']
        }
        # R83: the fault matrix REWRITES the projection/store (probe
        # boundary) and, in real mode, the reentry installs update the
        # install manifest. Offline their final state is not provable, so the
        # WRITING stage only marks them 'retained' (legal terminal, per-object
        # reason, never a deletion right); a failure AFTER the writes still
        # records the retention so the same run's cleanup closes over a clear
        # per-object record.
        $evidenceRels = @($contract['ProjectionRel'], 'share-store.db')
        if ($faultRoot -eq $resolvedRoot) {
            # the harness scenario root is an isolated temp root - it is NOT
            # this run's ledger-tracked manifest
            $evidenceRels += 'broker\install-state.json'
        }
        try {
            foreach ($fault in @('acl-change', 'task-register', 'projection-write', 'backup-cleanup')) {
                $verdicts = @()
                switch ($fault) {
                    'acl-change' {
                        $verdicts = @(Invoke-D4FaultScenarioAclChange -RunRoot $faultRoot `
                            -SourceRoot $sourceRoot -Cfg $brokerCfg -FailingIcacls $FailingIcaclsRunner `
                            -GoodIcacls $IcaclsRunner -SchtasksRunner $SchtasksRunner)
                    }
                    'backup-cleanup' {
                        $verdicts = @(Invoke-D4FaultScenarioBackupCleanup -RunRoot $faultRoot `
                            -SourceRoot $sourceRoot -Cfg $brokerCfg -GoodIcacls $IcaclsRunner `
                            -SchtasksRunner $SchtasksRunner)
                    }
                    'task-register' {
                        $verdicts = @(Invoke-D4FaultScenarioTaskRegister -RunRoot $faultRoot `
                            -SourceRoot $sourceRoot -Cfg $brokerCfg -GoodIcacls $IcaclsRunner `
                            -FailingSchtasks $FailingSchtasksRunner -GoodSchtasks $SchtasksRunner)
                    }
                    'projection-write' {
                        $faultSpec = @{ store = (Join-Path $resolvedRoot 'share-store.db');
                                        projection = (Join-Path $resolvedRoot $contract['ProjectionRel']);
                                        management_pipe = $contract['BrokerPipeName'];
                                        control_key = (Join-Path $resolvedRoot 'broker\broker-control-key');
                                        boundary = 'projection-write' } | ConvertTo-Json -Compress
                        $result = Invoke-CommandRunner -Runner $ProbeRunner -ArgumentList @('probe',
                            'fault-injection', '--spec', ('b64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($faultSpec))))
                        if ($result.ExitCode -ne 0) {
                            throw ("E4 failed at boundary projection-write: " + (@($result.Output) -join "`n"))
                        }
                        $verdicts = @(@($result.Output) -join "`n") -split '\s+'
                    }
                }
                # Per-boundary verdict contract: the refusal+recovery shape is
                # NOT one-size-fits-all. The backup-cleanup boundary is a
                # CRASH-GC recovery (state=consumed + a leftover backup file is
                # collected, never restored) - it has no refusal phase and the
                # reviewed scenario records 'gc-collected' as its second act.
                $boundaryExpectations = @{
                    'acl-change'       = @('refused', 'recovery-recorded')
                    'task-register'    = @('refused', 'recovery-recorded')
                    'projection-write' = @('refused', 'recovery-recorded')
                    'backup-cleanup'   = @('recovery-recorded', 'gc-collected')
                }
                foreach ($expected in @($boundaryExpectations[$fault])) {
                    if ($verdicts -notcontains $expected) {
                        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                            -Stage 'E4' -Verdict 'fail' -Detail ('boundary ' + $fault + ' lacks verdict ' + $expected) `
                            -FileBoundary $fsBoundary)
                        throw ("E4 failed: boundary " + $fault + " lacks verdict " + $expected)
                    }
                }
            }
        } catch {
            # R83: the fault matrix rewrites the projection/store (and, in
            # real mode, the install manifest) - the retention record keeps
            # the same run's cleanup closable over a clear per-object record
            # (an interrupted retention never deadlocks the cleanup).
            try {
                $ledger = Save-D4DynamicRetention -LedgerPath $ledgerPath -PackageHash $packageHash `
                    -RunId $ApprovedRunId -ExperimentRoot $resolvedRoot `
                    -RelativePaths $evidenceRels `
                    -Retention 'E4 fault matrix aborted; offline final-state evidence not provable; retained (never granted deletion)' `
                    -FileBoundary $fsBoundary
            } catch {
                Write-Log ("WARNING: dynamic retention record at the E4 abort failed: " + $_.Exception.Message)
            }
            try {
                [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
                    -Stage 'E4' -Verdict 'fail' -Detail ('E4 aborted: ' + $_.Exception.Message) -FileBoundary $fsBoundary)
            } catch {
                if ("$($_.Exception.Message)" -notlike '*already recorded*') {
                    Write-Log ("WARNING: E4 abort state record failed: " + $_.Exception.Message)
                }
            }
            throw
        }
        # R83: offline final-state evidence not provable - mark retained.
        $ledger = Save-D4DynamicRetention -LedgerPath $ledgerPath -PackageHash $packageHash `
            -RunId $ApprovedRunId -ExperimentRoot $resolvedRoot `
            -RelativePaths $evidenceRels `
            -Retention 'E4 fault matrix rewrote the projection/store; offline final-state evidence not provable; retained (never granted deletion)' `
            -FileBoundary $fsBoundary
        [void](Save-D4StageState -StatePath $statePath -PackageHash $packageHash -RunId $ApprovedRunId `
            -Stage 'E4' -Verdict 'pass' -Detail 'four audited fault boundaries: refusal + recovery recorded' `
            -FileBoundary $fsBoundary)
        Write-Output ("E4 complete for run " + $ApprovedRunId)
        exit 0
    }
    'Cleanup' {
        # R62-C: the ledger is the run's OWN ownership record - cleanup
        # stops/deletes ONLY identity-verified experiment tasks, removes ONLY
        # ledger-owned (hash-verified) objects, and lists everything it kept.
        # Mandatory evidence: a missing/failed collector is UNCONFIRMED and
        # the cleanup CANNOT pass.
        if (-not $NetStatRunner) {
            throw 'cleanup refused: no netstat collector supplied (port state UNCONFIRMED)'
        }
        if (-not $RulesRunner) {
            throw 'cleanup refused: no limit-rule collector supplied (rule state UNCONFIRMED)'
        }
        # 1. Load THIS run's ledger (absent = nothing was ever planned).
        $ledger = Read-D4IntentLedger -LedgerPath $ledgerPath -PackageHash $packageHash `
            -RunId $ApprovedRunId -FileBoundary $fsBoundary
        if ($null -ne $ledger) {
            # 2. R78-C: UNIFORM ledger pre-validation runs BEFORE any
            # stop/delete action: every entry carries a legal kind, a legal
            # status, and a path strictly inside the experiment root. ANY
            # violation refuses the WHOLE cleanup - zero Stop/Delete/
            # RemoveRule actions (the operator reviews the ledger).
            $knownKinds = @('dir', 'task', 'rule', 'acl', 'owned-copy', 'runtime-copy',
                            'deployment-fact', 'owned-secret', 'owned-wrapper',
                            'owned-artifact', 'owned-store', 'instance-fact',
                            'ownership-record', 'acl-recovery')
            $rootPrefix = $resolvedRoot.TrimEnd('\') + '\'
            $violations = @()
            foreach ($e in $ledger['Entries']) {
                $kind = [string]$e['kind']
                $path = [string]$e['path']
                $status = [string]$e['status']
                if ($kind -notin $knownKinds) {
                    $violations += ($path + ' (unknown ledger kind: ' + $kind + ')')
                    continue
                }
                if ($status -notin 'planned', 'created', 'removed', 'failed-clean', 'retained') {
                    $violations += ($path + ' (unknown ledger status: ' + $status + ')')
                    continue
                }
                if ($kind -in 'task', 'rule', 'acl') { continue }   # no filesystem path
                if (-not $path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    $violations += ($path + ' (ledger path escapes the experiment root)')
                    continue
                }
                try {
                    [void](Get-ManagedPath -TargetRoot $resolvedRoot `
                        -RelPath $path.Substring($rootPrefix.Length))
                } catch {
                    $violations += ($path + ' (ledger path refused by the whitelist: ' + $_.Exception.Message + ')')
                }
            }
            if ($violations.Count -gt 0) {
                throw ("cleanup ledger pre-validation FAILED (" + $violations.Count +
                       " bad records); NOTHING was touched: " + ($violations -join '; '))
            }
        }
        # 3. Identity-verified stops: only THIS run's expected tasks are
        # stopped (full definition match, /End confirmed by process exit
        # BEFORE /Delete). The logon context travels with EVERY spec
        # (§97.2: the verifier refuses a spec without one - the broker is
        # the SYSTEM/S4U identity).
        $brokerTask = @{ Name = $contract['BrokerTaskName']; IdentitySid = $contract['SystemSid'];
                         LogonType = 'S4U';
                         Command = (Join-Path $resolvedRoot 'run-broker.cmd'); Root = $resolvedRoot }
        [void](Stop-D4TaskObject -Task $brokerTask -Trigger 'ONDEMAND' `
            -SchtasksRunner $SchtasksRunner -ProcessEvidenceRunner $ProcessEvidenceRunner)
        if ($null -ne $config -and [string]$config.logon_method -in 'task-logon', 'password-managed') {
            # §93: the expected logon type travels with the spec - a task
            # registered with a DIFFERENT logon context (S4U vs Password) is
            # never claimed or deleted by this run.
            $shareLogon = if ([string]$config.logon_method -eq 'password-managed') { 'Password' } else { 'S4U' }
            $shareTask = @{ Name = $contract['ShareTaskName']; IdentitySid = [string]$config.share_sid;
                            LogonType = $shareLogon;
                            Command = (Join-Path $resolvedRoot 'run-live.cmd'); Root = $resolvedRoot }
            [void](Stop-D4TaskObject -Task $shareTask -Trigger 'ONDEMAND' `
                -SchtasksRunner $SchtasksRunner -ProcessEvidenceRunner $ProcessEvidenceRunner)
        }
        # 4. R67-B/C: remove THIS run's limit rules FIRST (identity-checked,
        # ABSENT confirmed per id - the closed create/remove loop); a foreign
        # rule is KEPT and listed. THEN the ledger-driven owned-object
        # removal, files first (every deletion needs a VALID recorded
        # content fingerprint that still matches), then the now-empty
        # directories in reverse depth. The uniform pre-validation already
        # refused any bad record BEFORE any stop/delete action (step 2).
        # The evidence directory stays as this run's record.
        $keptForeign = @()
        $keptRetained = @()
        # E1/W2: before the rule removal, reverse the python_home read grant
        # this run gave the share account (ledger-gated, ABSENT-verified).
        if ($null -ne $config -and [string]$config.logon_method -in 'task-logon', 'password-managed') {
            $pyHomeR = Invoke-D4RemoveSharePythonHomeAccess `
                -PythonHome ([string]$config.python_home) `
                -ShareSid ([string]$config.share_sid) `
                -IcaclsRunner $icaclsRunner -Ledger $ledger `
                -LedgerPath $ledgerPath -FileBoundary $fsBoundary
            $ledger = $pyHomeR['Ledger']
            $keptForeign += @($pyHomeR['KeptForeign'])
        }
        $limitsOut = Invoke-D4RemoveLimits -Inventory $inventory -RulesRunner $RulesRunner `
            -Ledger $ledger -LedgerPath $ledgerPath -FileBoundary $fsBoundary
        $ledger = $limitsOut['Ledger']
        $keptForeign += @($limitsOut['KeptForeign'])
        if ($null -ne $ledger) {
            # R78-C: the uniform ledger pre-validation already ran at step 2
            # (BEFORE any stop/delete action); this phase only deletes
            # hash-confirmed owned objects and lists what it kept.
            # 4b. FILES first: confirmed leaf objects only. A deletion REQUIRES
            # a valid recorded content fingerprint that still matches - an
            # empty/invalid recorded hash is an unconfirmed object that is
            # KEPT (never deleted on status alone).
            foreach ($e in $ledger['Entries']) {
                $kind = [string]$e['kind']
                if ($kind -in 'dir', 'task', 'rule', 'acl') { continue }
                $path = [string]$e['path']
                if ([string]$e['status'] -eq 'retained') {
                    # R83: dynamic artifacts deliberately kept - NEVER deleted,
                    # reported per object (the file's ownership/final state is
                    # not provable offline). A legal terminal, listed in its
                    # OWN report - distinct from foreign/unresolved keeps.
                    $keptRetained += ($path + " (retained dynamic artifact: " + [string]$e['retained_reason'] + ")")
                    continue
                }
                if ([string]$e['status'] -notin 'created', 'failed-clean') { continue }
                if (-not (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($path))) {
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'removed' -FileBoundary $fsBoundary
                    continue
                }
                $recorded = [string]$e['sha256_actual']
                if ($recorded -notmatch '^[0-9a-f]{64}$') {
                    # R67-C evidence gate: no valid fingerprint, no deletion.
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'failed-clean' -FileBoundary $fsBoundary
                    $keptForeign += ($path + ' (recorded content fingerprint missing/invalid - kept, unconfirmed)')
                    continue
                }
                $current = Invoke-D4File -Boundary $fsBoundary -Operation 'Hash' -Arguments @($path)
                if ($current -ne $recorded) {
                    # Content replaced after this run's creation is NOT this
                    # run's object anymore - kept, never deleted blindly.
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'failed-clean' -FileBoundary $fsBoundary
                    $keptForeign += ($path + " (content differs from this run's record, kept)")
                    continue
                }
                Invoke-D4File -Boundary $fsBoundary -Operation 'Remove' -Arguments @($path)
                $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                    -Path $path -Status 'removed' -FileBoundary $fsBoundary
            }
            # 4c. Directories in REVERSE depth: after the file phase every
            # owned dir can empty out, so a normal cleanup completes in ONE
            # pass; a non-empty dir is kept (never blind-recursed).
            $dirEntries = @($ledger['Entries'] | Where-Object {
                [string]$_['kind'] -eq 'dir' -and
                -not ([string]$_['path']).Equals($evidenceDir, [StringComparison]::OrdinalIgnoreCase) } |
                ForEach-Object {
                    $p = [string]$_['path']
                    @{ Entry = $_; Depth = ($p.TrimEnd('\').Split('\').Count) }
                } | Sort-Object -Property @{ Expression = { $_.Depth } } -Descending)
            foreach ($de in $dirEntries) {
                $e = $de['Entry']
                $path = [string]$e['path']
                if ([string]$e['status'] -notin 'created', 'failed-clean', 'retained') { continue }
                if (-not (Invoke-D4File -Boundary $fsBoundary -Operation 'Exists' -Arguments @($path))) {
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'removed' -FileBoundary $fsBoundary
                    continue
                }
                # A directory is removed ONLY when verifiably empty -
                # never blind-recursed over unknown content.
                $left = @(Invoke-D4File -Boundary $fsBoundary -Operation 'List' -Arguments @($path))
                $retainedContainer = $false
                if ($left.Count -gt 0) {
                    # R85: a container whose ONLY remaining children are legal
                    # 'retained' ledger objects is itself retained + reported -
                    # a real-mode E4 keeps broker\install-state.json, so the
                    # broker dir can never be emptied by the hash-gated
                    # deletion. The ACTUAL content is checked: any unknown
                    # child or failed-clean entry keeps the strict semantics
                    # (never exempted by the presence of one retained file,
                    # never force-deleted recursively).
                    $allChildrenRetained = $true
                    foreach ($child in $left) {
                        $childEntry = @($ledger['Entries'] | Where-Object {
                            [string]::Equals([string]$_['path'], [string]$child, [StringComparison]::OrdinalIgnoreCase) })[0]
                        if ($null -eq $childEntry -or [string]$childEntry['status'] -ne 'retained') {
                            $allChildrenRetained = $false
                            break
                        }
                    }
                    if ($allChildrenRetained) { $retainedContainer = $true }
                }
                if ($left.Count -eq 0) {
                    if ([string]$e['status'] -eq 'retained') {
                        # a retained container emptied afterwards: its vanished
                        # children already trip the retained-MISSING report -
                        # the dir itself stays retained (never re-deleted).
                        $keptRetained += ($path + ' (retained container; kept)')
                    } else {
                        Invoke-D4File -Boundary $fsBoundary -Operation 'Remove' -Arguments @($path)
                        $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                            -Path $path -Status 'removed' -FileBoundary $fsBoundary
                    }
                } elseif ($retainedContainer) {
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'retained' `
                        -Retention 'parent container of retained dynamic artifacts (offline final-state not provable); retained, never deleted' `
                        -FileBoundary $fsBoundary
                    $names = (@($left) | ForEach-Object { Split-Path $_ -Leaf }) -join ', '
                    $keptRetained += ($path + ' (retained container: ' + $names + ')')
                } else {
                    $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath `
                        -Path $path -Status 'failed-clean' -FileBoundary $fsBoundary
                    $names = (@($left) | ForEach-Object { Split-Path $_ -Leaf }) -join ', '
                    $keptForeign += ($path + ' (not empty, kept: ' + $names + ')')
                }
            }
        }
        # 4d. R71-B: the finalize NEVER claims a removal without proof -
        # per-object Exists confirmation; anything kept/unresolved is folded
        # into the final verdict (never a silent PASS).
        $aclFinal = Invoke-D4CompleteExperimentAcl -FileBoundary $fsBoundary `
            -EvidenceDir (Join-Path $resolvedRoot $contract['StateDirRel']) `
            -ExperimentRoot $resolvedRoot
        # 5. FRESH final evidence (after the stops/removals). The
        # experiment-wide collector (image path OR command line) is used - the
        # ShareImage-name filter variant cannot survive the runner's -Command
        # pass-through and would report every process as unconfirmed.
        $procEviFinal = Get-D4ExperimentProcessEvidence `
            -ExperimentRoot $resolvedRoot -Runner $ProcessEvidenceRunner
        $net = Invoke-CommandRunner -Runner $NetStatRunner -ArgumentList @('netstat', '-ano', '-p', 'TCP')
        $portEvi = @{ Unconfirmed = ($net.ExitCode -ne 0)
                      Listening = (($net.ExitCode -eq 0) -and
                                   ((@($net.Output | Where-Object { $_ -match ':\s*9100\s' -and $_ -match 'LISTENING' }).Count) -gt 0)) }
        # 6. Final verdict: rules REQUIRED, ledger REQUIRED, exit-confirmed
        # tasks/processes/listener. Kept foreign objects are LISTED (they do
        # not block - they are not this run's objects - but cleanup never
        # claims more than it verified).
        $missing = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract `
            -Ledger $ledger -EvidenceDir $evidenceDir `
            -SchtasksRunner $SchtasksRunner -ProcessEvidence $procEviFinal -PortEvidence $portEvi `
            -FileBoundary $fsBoundary -RulesRunner $RulesRunner)
        if (@($aclFinal['Unresolved']).Count -gt 0) {
            $missing += @($aclFinal['Unresolved'])
        }
        if ($missing.Count -gt 0) {
            throw ("cleanup incomplete (" + $missing.Count + " unconfirmed objects); evidence kept: " + ($missing -join '; '))
        }
        if ($keptRetained.Count -gt 0) {
            Write-Output ("cleanup RETAINED dynamic artifacts (offline final-state not provable; kept, never deleted): " + ($keptRetained -join '; '))
        }
        if ($keptForeign.Count -gt 0) {
            Write-Output ("cleanup KEPT foreign/unresolved objects (listed, not touched): " + ($keptForeign -join '; '))
        }
        Write-Output ("cleanup verified for run " + $ApprovedRunId + ": no experiment task/process/listener/rule; all owned objects resolved or deliberately retained")
        exit 0
    }
}
