# execute-d4-experiment-lib.ps1 - offline-testable machinery for the D4
# experiment orchestration (D4-P rework per handoff §62). Every OS edge is
# injectable: FileBoundary (exists/leaf/list/read/write/xml-write/remove/
# mkdir/hash/byte-copy), task/process/port/rule runners. The production entry wires real boundaries
# only under -ExecuteExperiment + an approved run id; the offline fixture
# drives the SAME functions through an in-memory fake OS model.
#
# Stage-state identity (R62-A): state files bind the FULL package hash
# (source closure + orchestration/install bodies + fixed runtime manifest)
# AND the run id; every read validates package hash + run id + structure,
# and the persisted form is always the JSON object (never hashtable
# Count/Keys properties).
#
# State-file producers (P3 contract):
#   deploy-mode.json           <- deployment/operator (explicit, never inferred)
#   um-share-projection.json   <- single-authority projection writer
#   guard-status.json          <- share_guard.py --run-dir (per-run TRANSIENT)
#   install-state.json         <- install-share-broker-lib (broker manifest)
#   task-definition.xml        <- install-share-broker-lib (definition artifact)
#   instance-registration.json <- share instance self-publish (run\live);
#                                addressing candidate only - the broker
#                                identity verifier re-proves each peer
#   intent-*.json / stage-state-*.json <- THIS library (experiment ledger)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'install-share-broker-lib.ps1')

function New-D4RealFileBoundary {
    # The REAL file boundary (used by the production entry). Offline
    # fixtures substitute an in-memory model with the same six operations.
    return @{
        Exists = { param([string]$Path) Test-Path -LiteralPath $Path }
        IsLeaf = { param([string]$Path) Test-Path -LiteralPath $Path -PathType Leaf }
        List = {
            param([string]$Path)
            if (-not (Test-Path -LiteralPath $Path)) { return @() }
            return @(Get-ChildItem -LiteralPath $Path -Force | ForEach-Object { $_.FullName })
        }
        ReadText = { param([string]$Path) [IO.File]::ReadAllText($Path) }
        WriteText = {
            param([string]$Path, [string]$Content)
            $parent = Split-Path -Parent $Path
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            # R73-A: the REAL write is ATOMIC (tmp in the same directory +
            # replace) - a crash mid-write can never leave a half-JSON
            # record; the recovery contract relies on this semantics.
            $tmp = $Path + '.tmp'
            [IO.File]::WriteAllText($tmp, $Content,
                (New-Object System.Text.UTF8Encoding($false)))
            if (Test-Path -LiteralPath $Path) {
                # PS 5.1 binds a literal $null to String.Empty for the String
                # backup-name parameter, and File.Replace rejects an EMPTY
                # backup path ("The path is not of a legal form"). NullString
                # passes a REAL null so the atomic rewrite works.
                [IO.File]::Replace($tmp, $Path, [NullString]::Value)
            } else {
                [IO.File]::Move($tmp, $Path)
            }
        }
        WriteXml = {
            # Task-Scheduler XML on this OS is only accepted with a BOM: a
            # UTF-8-no-BOM definition is rejected client-side by schtasks
            # ('cannot switch encoding'); the SYSTEM broker definition uses
            # the identical UTF-16LE-BOM form and registers fine. Same atomic
            # tmp+replace contract as WriteText, different encoding.
            param([string]$Path, [string]$Content)
            $parent = Split-Path -Parent $Path
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            $tmp = $Path + '.tmp'
            [IO.File]::WriteAllText($tmp, $Content,
                (New-Object System.Text.UnicodeEncoding($false, $true)))
            if (Test-Path -LiteralPath $Path) {
                [IO.File]::Replace($tmp, $Path, [NullString]::Value)
            } else {
                [IO.File]::Move($tmp, $Path)
            }
        }
        Remove = { param([string]$Path) Remove-Item -LiteralPath $Path -Force }
        MakeDir = {
            param([string]$Path)
            [void](New-Item -ItemType Directory -Path $Path -Force)
        }
        Hash = { param([string]$Path) Get-FileSha256 $Path }
        # R78-A: BYTE-EXACT copy for runtime/owned binaries - a text
        # ReadText->WriteText round-trip would decode/re-encode exe/DLL
        # bytes (and alter source BOM/encoding), corrupting the copy before
        # the hash gate can judge it. The copy refuses to overwrite an
        # existing landing spot (S1's own no-takeover check has already
        # guaranteed absence; this is the second, boundary-level guard).
        CopyBytes = {
            param([string]$Source, [string]$Destination)
            if (Test-Path -LiteralPath $Destination) {
                throw ("copy destination already exists (no takeover): " + $Destination)
            }
            $parent = Split-Path -Parent $Destination
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            [IO.File]::Copy($Source, $Destination, $false)
        }
    }
}

function Invoke-D4File {
    # Dispatches one boundary operation; a missing operation refuses.
    param([hashtable]$Boundary, [string]$Operation, [object[]]$Arguments)
    if (-not $Boundary.ContainsKey($Operation)) {
        throw ("file boundary operation not implemented: " + $Operation)
    }
    return (& $Boundary[$Operation] @Arguments)
}

function New-D4ExperimentContract {
    # §60.3 fixed object table. PURE: reviewed constants; NO free parameters.
    return @{
        ExperimentRoot           = 'C:\ProgramData\UMShareExp-broker'
        ProductionRoot           = 'C:\ProgramData\UMShareRun'
        SourceRootDefault        = 'D:\git\github\personal_files\SelfUseAI\ai-gateway'
        BrokerTaskName           = 'umshare-broker-exp'
        ShareTaskName            = 'umshare-share-exp'
        BrokerPipeName           = '\\.\pipe\um-share-broker-exp'
        InstancePipePrefix       = '\\.\pipe\um-share-instance-exp-'
        ProductionBrokerPipe     = '\\.\pipe\um-share-broker'
        ProductionInstancePrefix = '\\.\pipe\um-share-instance-'
        ProductionBrokerTask     = 'umshare-broker'
        ProductionShareTask      = 'umshare-live'
        SharePort                = 9100
        ShareImage               = 'um-share-python.exe'
        SystemSid                = 'S-1-5-18'
        RunLayout                = @('image', 'runtime', 'broker', 'run\live', 'evidence')
        RegistrationRel          = 'run\live\instance-registration.json'
        GuardRunRel              = 'run\live'
        DeployModeRel            = 'deploy-mode.json'
        ProjectionRel            = 'um-share-projection.json'
        StateDirRel              = 'evidence'
        TaskTrigger              = 'ONDEMAND'
        # §93 password-managed bootstrapping: the account bootstrap record
        # lives OUTSIDE the experiment root (S0's namespace check must never
        # see it as experiment content) and OUTSIDE production.
        BootstrapRoot            = 'C:\ProgramData\UMShareExp-bootstrap'
        BootstrapStateRel        = 'bootstrap-state-<runid>.json'
        RecoveryStateRel         = 'bootstrap-recovery-<runid>.json'
        BootstrapAccountDefault  = 'um_share_d4'
        ModuleClosure            = @(
            'chat2api\share_gateway.py', 'um-share-deploy\share_guard.py',
            'share_instance_pipe.py', 'share_broker_server.py',
            'share_broker_host.py', 'share_broker_protocol.py',
            'share_runtime.py', 'share_management.py', 'share_store.py',
            'share_broker_client.py', 'share_broker_ops.py',
            'um-share-deploy\d4_stage_probe.py',
            'um-share-deploy\d4-account-bootstrap.ps1'
        )
    }
}

function Test-D4RunIdFormat {
    # R62-A: a bounded identifier with no path/shell meaning.
    param([string]$RunId)
    if (-not $RunId) { throw 'run id is required (real mode uses the conversation-approved id verbatim)' }
    if ($RunId -notmatch '^[0-9A-Za-z-]{8,64}$') {
        throw ("run id must match [0-9A-Za-z-]{8,64} (no path/shell characters, 8..64 long): '" + $RunId + "'")
    }
    return $true
}

# R67-A: fixed WFP identity of the experiment isolation pattern. The layer
# GUIDs / action codes / condition field GUIDs are the reviewed constants
# (fwpmu.h; cross-checked against the reviewed wfp_helper.ps1 semantics):
# permit = app-id + 127.0.0.1 + share-port, block = app-id ONLY.
$script:D4WfpLayerGuids = @{
    'connect-v4' = 'c38d57d1-05a7-4c33-904f-7fbceee60e82'
    'connect-v6' = '4a72393b-319f-44bc-84c3-ba54dcb3b6b4'
}
$script:D4WfpActionCode = @{ 'permit' = 0x1002; 'block' = 0x1001 }
# R78-D: the FWP return codes the runner matches MUST be pinned as UNSIGNED
# constants - the real P/Invoke marshals uint32 (PS renders it as a positive
# Int64), while PS 5.1 parses the hex literals as NEGATIVE Int32, so a raw
# '$rc -eq 0x80320003' can never match a real return value (the fake-native
# decision fixtures surfaced this). Values: 0x80320003 = the BFE 'not found'
# code, 0x80320009 = the BFE 'already exists' code.
$script:D4FwpRcFilterNotFound = [uint32]2150760451
$script:D4FwpRcAlreadyExists = [uint32]2150760457
$script:D4WfpAppIdField = 'd78e1e87-8644-4ea5-9437-d809ecefc971'
$script:D4WfpAddrField = 'b235ae9a-1d64-49b8-a44c-5ff3d9095045'
$script:D4WfpPortField = 'c35a604d-d22b-4e1a-91b4-68f674ee674b'
$script:D4WfpSubLayer = 'eebecc03-ced4-4380-819a-2734397b2b74'

function ConvertTo-D4RuleSpec {
    # The config arrives as JSON pscustomobjects; normalize ONE rule spec
    # into a plain hashtable with EXACTLY the fixed keys (unknown keys
    # refuse - a fixed structure slot is never silently widened).
    param([object]$Raw)
    $allowed = @('id', 'layer', 'action', 'app_path', 'remote_addr', 'remote_port')
    $spec = @{}
    $props = if ($Raw -is [hashtable]) { @($Raw.Keys) } else { @($Raw.PSObject.Properties | ForEach-Object { $_.Name }) }
    foreach ($k in $props) {
        if ($k -notin $allowed) {
            throw ("limit rule spec has an unknown key (fixed structure only): " + $k)
        }
        $spec[$k] = if ($Raw -is [hashtable]) { $Raw[$k] } else { $Raw.PSObject.Properties[$k].Value }
    }
    return $spec
}

function Test-D4RuleSpec {
    # R67-A: ONE limit rule is an EXACT rule specification - never a bare
    # GUID. The reviewed experiment isolation pattern (app-scoped): the
    # permit rule carries the loopback share-port scope (app-id + 127.0.0.1
    # + the fixed share port), every block rule carries ONLY the app-id
    # condition (any narrowing of a block = tamper/mis-scope). Unknown keys
    # refuse; the port/layer/action are bound to the fixed experiment
    # contract. Returns the NORMALIZED spec (hashtable) on success.
    param([object]$RawSpec, [hashtable]$Contract)
    $spec = ConvertTo-D4RuleSpec -Raw $RawSpec
    foreach ($k in @('id', 'layer', 'action', 'app_path')) {
        if (-not $spec.ContainsKey($k)) {
            throw ("limit rule spec missing required field: " + $k)
        }
    }
    $id = [string]$spec['id']
    try { [void][Guid]::Parse($id) } catch {
        throw ("limit rule id must be a GUID: " + $id)
    }
    if ([string]$spec['layer'] -notin @($script:D4WfpLayerGuids.Keys)) {
        throw ("limit rule layer must be one of: " + (($script:D4WfpLayerGuids.Keys | Sort-Object) -join ', '))
    }
    if ([string]$spec['action'] -notin 'permit', 'block') {
        throw "limit rule action must be 'permit' or 'block'"
    }
    if (-not [string]$spec['app_path']) {
        throw 'limit rule app_path is required (the experiment image path)'
    }
    $isPermit = ([string]$spec['action'] -eq 'permit')
    $hasAddr = $spec.ContainsKey('remote_addr') -and [string]$spec['remote_addr']
    $hasPort = $spec.ContainsKey('remote_port') -and $null -ne $spec['remote_port'] -and [string]$spec['remote_port'] -ne ''
    if ($isPermit) {
        # The reviewed isolation pattern: the ONLY permitted destination is
        # the loopback share port - the scope is not caller-chosen.
        if (-not $hasAddr -or -not $hasPort) {
            throw "a permit limit rule must carry remote_addr + remote_port (the fixed loopback share-port scope)"
        }
        if ([string]$spec['remote_addr'] -ne '127.0.0.1') {
            throw ("a permit limit rule's remote_addr is fixed to the loopback share address, got: " + [string]$spec['remote_addr'])
        }
        if ([int]$spec['remote_port'] -ne $Contract['SharePort']) {
            throw ("a permit limit rule's remote_port must be the fixed experiment port " + $Contract['SharePort'] + ", got: " + [int]$spec['remote_port'])
        }
    } elseif ($hasAddr -or $hasPort) {
        # A BLOCK rule carries ONLY the app-id condition: any addr/port
        # narrowing silently re-opens the upstream - never accepted.
        throw 'a block limit rule must carry ONLY the app-id condition (no remote_addr/remote_port)'
    }
    return $spec
}

function Test-D4ExperimentConfig {
    # R62-A/D: the experiment config is a FIXED structure (unknown keys
    # refuse; every action-affecting slot is required). Collected identity
    # is a D4-X collection input - placeholder values can NEVER pass a real
    # branch; runtime/limits are required slots, never guessed.
    param([hashtable]$Config, [hashtable]$Contract)
    $required = @('run_id', 'identity_source', 'operator_account',
                  'operator_sid', 'share_account', 'share_sid',
                  'logon_method', 'runtime_source_dir', 'runtime_files',
                  'python_home', 'broker_pipe_sddl', 'instance_pipe_sddl',
                  'instance_pipe_prefix', 'share_port', 'probe', 'auth_mode',
                  'limit_rules')
    foreach ($k in $required) {
        if (-not $config.ContainsKey($k)) {
            throw ("experiment config missing required slot: " + $k)
        }
    }
    foreach ($k in @($config.Keys)) {
        if ($k -notin $required) {
            throw ("experiment config has an unknown slot (fixed structure only): " + $k)
        }
    }
    [void](Test-D4RunIdFormat ([string]$config['run_id']))
    if ([string]$config['instance_pipe_prefix'] -ne $contract['InstancePipePrefix']) {
        throw ("experiment config instance pipe prefix must be the fixed experiment prefix: " + [string]$config['instance_pipe_prefix'])
    }
    if ([int]$config['share_port'] -ne $contract['SharePort']) {
        throw ("experiment config share port must be " + $contract['SharePort'])
    }
    if ($config['probe'] -ne $true) {
        throw 'experiment config probe must be true'
    }
    if ([string]$config['auth_mode'] -ne 'projection') {
        throw "experiment config auth_mode must be 'projection'"
    }
    if ([string]$config['logon_method'] -notin 'task-logon', 'password-managed', 'unestablished') {
        throw ("experiment config logon_method must be 'task-logon', 'password-managed' or 'unestablished' " +
               "(the share task logon is a collected/registered fact, never guessed)")
    }
    foreach ($slot in @('broker_pipe_sddl', 'instance_pipe_sddl')) {
        $v = [string]$config[$slot]
        if (-not $v.Trim()) { throw ("experiment config slot must be non-empty: " + $slot) }
        if ($v -match 'PLACEHOLDER|S-1-5-18-exp|TODO') {
            throw ("experiment config slot carries a placeholder DACL (calibrated values are a collected input): " + $slot)
        }
    }
    if (-not [string]$config['runtime_source_dir']) {
        throw 'experiment config runtime_source_dir is required (the isolated runtime copy source)'
    }
    $runtimeFiles = @($config['runtime_files'])
    if ($runtimeFiles.Count -eq 0) {
        throw 'experiment config runtime_files must list the isolated runtime manifest (image + DLL closure), never guessed'
    }
    foreach ($rf in $runtimeFiles) {
        if (-not $rf.file -or -not $rf.sha256 -or $rf.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw ("experiment runtime file entry needs file + sha256(64hex): " + $rf)
        }
    }
    $limitRules = @($config['limit_rules'])
    if ($limitRules.Count -eq 0) {
        throw 'experiment config limit_rules is required (the exact experiment rule specifications, never bare GUIDs)'
    }
    $normRules = @()
    foreach ($rawRule in $limitRules) {
        # R67-A: each entry is an EXACT rule spec (layer/action/app-id/scope);
        # the port/addr are bound to the fixed experiment contract - a rule
        # that merely names a GUID can never describe isolation.
        $normRules += (Test-D4RuleSpec -RawSpec $rawRule -Contract $contract)
    }
    # R78-D: the rule SET is the reviewed isolation TRIPLE validated as a
    # whole, not merely per-entry: exactly permit-v4 + block-v4 + block-v6
    # (a partial set - only the permit, a missing block-v4 or block-v6 -
    # can never prove the default-deny outbound isolation), unique ids,
    # ONE consistent image across every rule, and that image IS the fixed
    # experiment image.
    if ($normRules.Count -ne 3) {
        throw ("experiment config limit_rules must be EXACTLY the reviewed isolation triple (permit-v4/block-v4/block-v6), got " + $normRules.Count + " rules")
    }
    $seenRuleIds = @{}
    foreach ($rule in $normRules) {
        $rid = ([string]$rule['id']).ToLowerInvariant()
        if ($seenRuleIds.ContainsKey($rid)) {
            throw ("experiment config limit_rules carries a DUPLICATE rule id (every rule needs its own id): " + $rid)
        }
        $seenRuleIds[$rid] = $true
    }
    $roleSet = @($normRules | ForEach-Object { ([string]$_['layer']) + '/' + ([string]$_['action']) } | Sort-Object)
    $expectedRoleSet = @('connect-v4/block', 'connect-v4/permit', 'connect-v6/block')
    if (($roleSet -join ',') -ne ($expectedRoleSet -join ',')) {
        throw ("experiment config limit_rules must be EXACTLY one connect-v4/permit + one connect-v4/block + one connect-v6/block (the reviewed isolation triple), got: " + ($roleSet -join ','))
    }
    $ruleImages = @($normRules | ForEach-Object { [string]$_['app_path'] } | Select-Object -Unique)
    if ($ruleImages.Count -ne 1) {
        throw ("experiment config limit_rules must carry ONE consistent image across all rules, got: " + ($ruleImages -join ', '))
    }
    $imagePath = Join-Path $contract['ExperimentRoot'] $contract['ShareImage']
    if ($ruleImages[0] -ne $imagePath) {
        throw ("experiment config limit_rules app_path must be the FIXED experiment image " + $imagePath + ", got: " + $ruleImages[0])
    }
    if ([string]$config['identity_source'] -eq 'collected') {
        foreach ($slot in @('operator_account', 'operator_sid',
                            'share_account', 'share_sid')) {
            if (-not [string]$config[$slot]) {
                throw ("experiment config collected identity requires a non-empty " + $slot)
            }
        }
    } elseif ([string]$config['identity_source'] -ne 'pending-collection') {
        throw "experiment config identity_source must be 'collected' or 'pending-collection'"
    }
    return $true
}

function Get-D4PackageHash {
    # R62-A: binds EVERYTHING that can change actions - the source closure,
    # the orchestration/install bodies, and the fixed runtime manifest.
    param([hashtable]$Contract, [hashtable]$ClosureManifest,
          [string]$SourceRoot, [string]$RuntimeManifestJson)
    $parts = @()
    foreach ($e in $ClosureManifest['Entries']) {
        $parts += ('closure/' + $e.File + '=' + $e.Sha256)
    }
    foreach ($rel in @('um-share-deploy\execute-d4-experiment-lib.ps1',
                       'um-share-deploy\execute-d4-experiment.ps1',
                       'um-share-deploy\install-share-broker-lib.ps1',
                       'um-share-deploy\install-share-broker.ps1',
                       'um-share-deploy\d4-account-bootstrap.ps1',
                       'um-share-deploy\share_guard.py',
                       'chat2api\share_gateway.py')) {
        $f = Join-Path $SourceRoot $rel
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) {
            throw ("package file missing: " + $f)
        }
        $parts += ('code/' + $rel + '=' + (Get-FileSha256 $f))
    }
    $parts += ('runtime-manifest=' + $RuntimeManifestJson)
    $canonical = ($parts | Sort-Object) -join "`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($canonical)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Test-D4Isolation {
    # §60.3: resolved root must be EXACTLY the fixed experiment root, never
    # equal/inside/containing the production root; root + all ancestors
    # reparse-free. $ItemProbe injectable (R51.6 contract).
    param([hashtable]$Contract, [string]$ResolvedRoot, [scriptblock]$ItemProbe)
    if (-not $ResolvedRoot) {
        throw 'execute-d4-experiment: the experiment root must resolve to an absolute path'
    }
    $fixedRoot = $Contract['ExperimentRoot']
    if (-not $ResolvedRoot.Equals($fixedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw ("execute-d4-experiment: the experiment root is FIXED to '" + $fixedRoot +
               "' (got '" + $ResolvedRoot + "'); a different root is a different reviewed contract")
    }
    $prod = $Contract['ProductionRoot']
    $cmp = [StringComparison]::OrdinalIgnoreCase
    if ($ResolvedRoot.Equals($prod, $cmp)) {
        throw ("execute-d4-experiment: experiment root collides with PRODUCTION root: " + $prod)
    }
    $rootWithSep = $ResolvedRoot.TrimEnd('\') + '\'
    $prodWithSep = $prod.TrimEnd('\') + '\'
    if ($prodWithSep.StartsWith($rootWithSep, $cmp)) {
        throw ("execute-d4-experiment: the production root sits INSIDE the experiment root: " + $prod)
    }
    if ($rootWithSep.StartsWith($prodWithSep, $cmp)) {
        throw ("execute-d4-experiment: the experiment root sits INSIDE the production root: " + $ResolvedRoot)
    }
    $current = $ResolvedRoot
    while ($true) {
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
        $probe = & $ItemProbe $current
        if ($null -ne $probe -and
                ($probe.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw ("execute-d4-experiment: ancestor reparse point refused: " + $current)
        }
    }
    $rootItem = & $ItemProbe $ResolvedRoot
    if ($null -ne $rootItem -and
            ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw ("execute-d4-experiment: experiment root is a reparse point - refusing: " + $ResolvedRoot)
    }
    return $true
}

function Get-D4SourceClosureManifest {
    # The copy manifest = the contract's reviewed module closure (repo-
    # relative sources; the run copy lands FLAT). A missing source refuses.
    param([hashtable]$Contract, [string]$SourceRoot, [hashtable]$FileBoundary)
    $rows = @()
    foreach ($rel in $Contract['ModuleClosure']) {
        $src = Join-Path $SourceRoot $rel
        if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($src))) {
            throw ("execute-d4-experiment: source closure file missing: " + $src)
        }
        $rows += @{ File = (Split-Path $src -Leaf); Rel = $rel; Src = $src;
                    Sha256 = (Invoke-D4File -Boundary $FileBoundary -Operation 'Hash' -Arguments @($src)) }
    }
    return @{ Entries = $rows }
}

function New-D4ObjectInventory {
    # §60.4-3: the COMPLETE experiment object list, produced BEFORE any
    # action; every path is inside the experiment root.
    param([hashtable]$Contract, [hashtable]$ClosureManifest,
          [hashtable]$Config, [string]$PackageHash)
    $root = $Contract['ExperimentRoot']
    $runId = [string]$Config['run_id']
    $dirs = @($Contract['RunLayout'] | ForEach-Object { Join-Path $root $_ })
    $files = @()
    foreach ($e in $ClosureManifest['Entries']) {
        $files += @{ Path = (Join-Path $root $e.File); Kind = 'owned-copy'; Source = $e.Src; Sha256 = $e.Sha256 }
    }
    $files += @{ Path = (Join-Path $root $Contract['DeployModeRel']); Kind = 'deployment-fact'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (explicit mode=projection)' }
    $files += @{ Path = (Join-Path $root $Contract['ProjectionRel']); Kind = 'deployment-fact'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (test projection, random secret hash)' }
    $files += @{ Path = (Join-Path $root 'broker\broker-control-key'); Kind = 'owned-secret'; Source = ''; Sha256 = ''; Producer = 'Install-Broker' }
    $files += @{ Path = (Join-Path $root 'instance-channel-key'); Kind = 'owned-secret'; Source = ''; Sha256 = ''; Producer = 'Install-Broker' }
    $files += @{ Path = (Join-Path $root 'run-broker.cmd'); Kind = 'owned-wrapper'; Source = ''; Sha256 = ''; Producer = 'Install-Broker' }
    $files += @{ Path = (Join-Path $root 'broker\task-definition.xml'); Kind = 'owned-artifact'; Source = ''; Sha256 = ''; Producer = 'Install-Broker (definition document)' }
    $files += @{ Path = (Join-Path $root 'run-live.cmd'); Kind = 'owned-wrapper'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (share guard capture script)' }
    if ([string]$config['logon_method'] -in 'task-logon', 'password-managed') {
        $files += @{ Path = (Join-Path $root 'run-live-task-definition.xml'); Kind = 'owned-artifact'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (share task definition document)' }
    }
    $files += @{ Path = (Join-Path $root 'experiment-config.json'); Kind = 'deployment-fact'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (validated fixed-structure experiment config; the guard launch-pinning consumer)' }
    $files += @{ Path = (Join-Path $root 'share-store.db'); Kind = 'owned-store'; Source = ''; Sha256 = ''; Producer = 'E3 (isolated test store, initialized by the real ShareStore)' }
    $files += @{ Path = (Join-Path $root $Contract['RegistrationRel']); Kind = 'instance-fact'; Source = ''; Sha256 = ''; Producer = 'share instance self-publish (addressing candidate; the broker identity verifier re-proves each peer)' }
    # W2 run21: the per-run TRANSIENT status telemetry is a PLANNED dynamic
    # artifact exactly like the registration fact (no fingerprint - the
    # runtime writes it AFTER the ledger snapshot). Without a planned entry
    # the cleanup's R85 container check sees an UNKNOWN child and the broker/
    # run-live dirs can never resolve (observed: two failed-clean dirs +
    # one planned-takeover verdict blocked the first qualified E1 cleanup).
    $files += @{ Path = (Join-Path $root 'broker\broker-status.json'); Kind = 'owned-artifact'; Source = ''; Sha256 = ''; Producer = 'share_broker_server per-cycle status telemetry (per-run TRANSIENT)' }
    $files += @{ Path = (Join-Path $root ($Contract['GuardRunRel'] + '\guard-status.json')); Kind = 'owned-artifact'; Source = ''; Sha256 = ''; Producer = 'share_guard.py per-run TRANSIENT guard status' }
    $files += @{ Path = (Join-Path $root 'broker\install-state.json'); Kind = 'ownership-record'; Source = ''; Sha256 = ''; Producer = 'Install-Broker' }
    $files += @{ Path = (Join-Path $root 'broker-pre-acl.dat'); Kind = 'acl-recovery'; Source = ''; Sha256 = ''; Producer = 'Install-Broker (transient)' }
    foreach ($rf in @($config['runtime_files'])) {
        $files += @{ Path = (Join-Path $root ([string]$rf.file)); Kind = 'runtime-copy';
                     Source = (Join-Path ([string]$config['runtime_source_dir']) ([string]$rf.file));
                     Sha256 = [string]$rf.sha256; Producer = 'S1 prepare (isolated runtime, hash-verified)' }
    }
    $files += @{ Path = (Join-Path $root ($Contract['StateDirRel'] + '\intent-' + $PackageHash.Substring(0, 8) + '-' + $runId + '.json')); Kind = 'intent-ledger'; Source = ''; Sha256 = ''; Producer = 'S1 prepare (written BEFORE any object)' }
    $files += @{ Path = (Join-Path $root ($Contract['StateDirRel'] + '\stage-state-' + $PackageHash.Substring(0, 8) + '-' + $runId + '.json')); Kind = 'stage-state'; Source = ''; Sha256 = ''; Producer = 'execute-d4-experiment-lib' }
    $tasks = @(
        @{ Name = $Contract['BrokerTaskName']; Identity = 'SYSTEM'; IdentitySid = $contract['SystemSid'];
           LogonType = 'S4U'; Trigger = $contract['TaskTrigger'];
           Command = (Join-Path $root 'run-broker.cmd'); Definition = 'broker\task-definition.xml' }
    )
    if ([string]$config['logon_method'] -in 'task-logon', 'password-managed') {
        $tasks += @{ Name = $contract['ShareTaskName']; Identity = [string]$config['share_account'];
                     IdentitySid = [string]$config['share_sid'];
                     LogonType = $(if ([string]$config['logon_method'] -eq 'password-managed') { 'Password' } else { 'S4U' });
                     Trigger = 'ONDEMAND'; Command = (Join-Path $root 'run-live.cmd');
                     Definition = 'run-live-task-definition.xml (S1, requires collected share identity)' }
    }
    return @{
        RunId       = $runId
        PackageHash = $PackageHash
        Root        = $root
        Dirs        = $dirs
        Files       = $files
        Tasks       = $tasks
        Pipes       = @(
            @{ Name = $Contract['BrokerPipeName']; Side = 'management'; Sddl = [string]$Config['broker_pipe_sddl'] },
            @{ Name = ($Contract['InstancePipePrefix'] + '<instance_id>'); Side = 'instance'; Sddl = [string]$Config['instance_pipe_sddl'] }
        )
        Port        = [int]$Config['share_port']
        Image       = (Join-Path $root $Contract['ShareImage'])
        # R67-A: the limits are the EXACT rule specifications (identity +
        # semantics), not bare GUIDs; the id list is derived for the
        # ABSENT/PRESENT verdict checks and the intent ledger.
        RuleSpecs   = @($Config['limit_rules'])
        RuleIds     = @(@($Config['limit_rules']) | ForEach-Object {
            if ($_ -is [hashtable]) { [string]$_['id'] } else { [string]$_.id } })
    }
}

function New-D4ExperimentConfig {
    # §93: build the fixed-structure experiment config AFTER the bootstrap
    # created the share account and its SID is KNOWN (identity_source=
    # 'collected'). Pure + deterministic; the SID-bearing SDDL strings are
    # composed HERE so the config can never carry a placeholder identity.
    # NO secret field exists in the config (the share credential is never
    # part of it - the task scheduler owns the stored task credential).
    param(
        [hashtable]$Contract,
        [string]$RunId,
        [string]$OperatorAccount, [string]$OperatorSid,
        [string]$ShareAccount, [string]$ShareSid,
        [string]$PythonHome, [string]$RuntimeSourceDir,
        [object[]]$RuntimeFiles,
        [string[]]$RuleIds
    )
    [void](Test-D4RunIdFormat $RunId)
    if (-not $OperatorAccount -or -not $OperatorSid -or -not $ShareAccount -or -not $ShareSid) {
        throw 'New-D4ExperimentConfig: collected operator+share identity (account+SID) is required'
    }
    if (-not $PythonHome -or -not $RuntimeSourceDir) {
        throw 'New-D4ExperimentConfig: python home and runtime source dir are required'
    }
    if (@($RuntimeFiles).Count -eq 0) {
        throw 'New-D4ExperimentConfig: runtime_files must list the isolated runtime manifest'
    }
    foreach ($rf in @($RuntimeFiles)) {
        if (-not $rf.file -or -not $rf.sha256 -or $rf.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw ("New-D4ExperimentConfig: runtime file entry needs file+sha256(64hex): " + $rf)
        }
    }
    if (@($RuleIds).Count -ne 3) {
        throw 'New-D4ExperimentConfig: exactly 3 new rule GUIDs required (permit-v4, block-v4, block-v6)'
    }
    $seen = @{}
    foreach ($rid in @($RuleIds)) {
        try { [void][Guid]::Parse($rid) } catch {
            throw ("New-D4ExperimentConfig: rule id must be a GUID: " + $rid)
        }
        $low = $rid.ToLowerInvariant()
        if ($seen.ContainsKey($low)) {
            throw ("New-D4ExperimentConfig: duplicate rule id: " + $rid)
        }
        $seen[$low] = $true
    }
    # The reviewed SDDL shapes (recorded in output/d4x-20260910-01/
    # experiment-config-notes.md): management pipe = SYSTEM GA + operator
    # GRGW + share DENY + ANONYMOUS DENY; instance pipe = share GA + SYSTEM
    # GRGW. The share SID is the ONLY identity that changes per run.
    $brokerPipeSddl = ('D:P(A;;GA;;;SY)(A;;GRGW;;;' + $OperatorSid +
                       ')(D;;GA;;;' + $ShareSid + ')(D;;GA;;;AN)')
    $instancePipeSddl = ('D:P(A;;GA;;;' + $ShareSid + ')(A;;GRGW;;;SY)')
    $imagePath = Join-Path $Contract['ExperimentRoot'] $Contract['ShareImage']
    $limitRules = @(
        @{ id = $RuleIds[0]; layer = 'connect-v4'; action = 'permit';
           app_path = $imagePath; remote_addr = '127.0.0.1'; remote_port = [int]$Contract['SharePort'] },
        @{ id = $RuleIds[1]; layer = 'connect-v4'; action = 'block'; app_path = $imagePath },
        @{ id = $RuleIds[2]; layer = 'connect-v6'; action = 'block'; app_path = $imagePath }
    )
    return @{
        run_id = $RunId
        identity_source = 'collected'
        operator_account = $OperatorAccount
        operator_sid = $OperatorSid
        share_account = $ShareAccount
        share_sid = $ShareSid
        logon_method = 'password-managed'
        runtime_source_dir = $RuntimeSourceDir
        runtime_files = @($RuntimeFiles)
        python_home = $PythonHome
        broker_pipe_sddl = $brokerPipeSddl
        instance_pipe_sddl = $instancePipeSddl
        instance_pipe_prefix = $Contract['InstancePipePrefix']
        share_port = [int]$Contract['SharePort']
        probe = $true
        auth_mode = 'projection'
        limit_rules = $limitRules
    }
}

function New-D4StagePlan {
    # §60.5 ordered stages with budgets and stop conditions (data only).
    # T0 (account bootstrap) is NOT part of the stage-state machine - its
    # success lives in the separate bootstrap record (absent -> creation-intent
    # -> created -> task-registered); S0/S1/E1 still gate each other via the
    # package+run-bound stage state.
    return @(
        @{ Stage = 'T0'; Budget = 'account bootstrap (one elevated process)';
           Title = '实验账户/凭据引导：只读预检+创建实验账户(um_share_d4)+强密码任务注册+实际SID配置；密码只存在注册进程内，不入脚本/配置/日志/argv';
           StopCondition = 'foreign account / unconfirmed query / registration failure -> stop, keep the bootstrap record, never start S0/S1/E1' },
        @{ Stage = 'S0'; Budget = 'read-only inventory + declared evidence write';
           Title = '只读盘点（三态）：根内容/两任务/9100/映像依赖/身份可用性；占用或不可确认即停，禁止清场';
           StopCondition = 'any unknown object or unconfirmed check -> stop, never clear or overwrite' },
        @{ Stage = 'S1'; Budget = 'prepare';
           Title = '实验准备：参数/身份/runtime 先验证；意图台账先落盘；逐对象创建并记录归属；broker 复用已审安装；share 任务需已采身份；不启动';
           StopCondition = 'any validation/copy/hash/task failure -> bounded recovery, list every created object, no task start' },
        @{ Stage = 'E1'; Budget = 'about 30 minutes';
           Title = '权限与合法启动：限制未就绪零启动；占位/非法SDDL负例拒绝；正确DACL启动受限share probe+SYSTEM broker；身份/映像/回环9100核对；管理/实例通道正反例（真实协议模块，OS边界注入）';
           StopCondition = 'any channel failure -> stop later stages, keep sanitized errors' },
        @{ Stage = 'E2'; Budget = 'about 20 minutes';
           Title = '真实资源取消：单连接静默/滴字节/阻塞读写（每类期限+默认最多3次）；句柄/线程/admission计数；超时不驱动迟到副作用';
           StopCondition = 'worker exit unconfirmable -> record placeholder + verify refusal at limit, never claim reclaimed' },
        @{ Stage = 'E3'; Budget = 'about 30 minutes';
           Title = '投影与换代闭环：真实store发布→writer→加载→ACK(generation/fingerprint/id)→9100正确/错误Key→撤销/换代→门闩暂停ACK→A→B换代→drain/emergency';
           StopCondition = 'stale ACK advanced or registration unverified -> stop' },
        @{ Stage = 'E4'; Budget = 'about 30 minutes';
           Title = '故障与回收：只对实验对象注入；ACL/备份清理/任务注册/投影写入各一个已审故障点；重入/卸载恢复核对；按清单停止删除实验对象';
           StopCondition = 'cleanup unverifiable -> stop and KEEP evidence; list every uncleaned object' },
        @{ Stage = 'Cleanup'; Budget = 'bounded';
           Title = '统一停止/清理：完整任务定义身份核对+退出确认，进程/端口证据必填，规则终态核对；只命中实验对象清单';
           StopCondition = 'cannot confirm -> stop, keep evidence, list every uncleaned object' }
    )
}

function New-D4ShareLaunchArgs {
    # P2 (D4-P §60.2): the share child's extra argv, composed from the
    # reviewed experiment contract. BYTE-FOR-BYTE the same string the guard
    # re-derives from the experiment config (share_guard.build_experiment_
    # share_args) - the guard rejects any env whose value differs, so a
    # tampered UM_SHARE_ARGS can never redirect the instance pipe. Two
    # pipes, two DACLs: the instance SDDL is REQUIRED and must never be the
    # production prefix or a default descriptor. Pure function.
    param([hashtable]$Contract, [string]$InstanceKeyPath,
          [string]$RegistrationPath, [string]$InstancePipeSddl)
    if (-not $InstanceKeyPath) {
        throw 'New-D4ShareLaunchArgs: instance key path is required'
    }
    if (-not $RegistrationPath) {
        throw 'New-D4ShareLaunchArgs: registration path is required'
    }
    if (-not $InstancePipeSddl -or -not $InstancePipeSddl.Trim()) {
        throw 'New-D4ShareLaunchArgs: instance pipe SDDL is required (never a default security descriptor)'
    }
    # The experiment prefix is the FIXED §60.3 namespace; a contract whose
    # prefix is anything else (the production prefix is the realistic tamper)
    # refuses - the share child can never be pointed at production pipes.
    $prefix = [string]$Contract['InstancePipePrefix']
    if (-not $prefix.StartsWith('\\.\pipe\um-share-instance-exp-', [StringComparison]::Ordinal)) {
        throw ("New-D4ShareLaunchArgs: instance pipe prefix must start with the experiment prefix '\\.\pipe\um-share-instance-exp-', got: " + $prefix)
    }
    return ('--instance-key-file "' + $InstanceKeyPath +
            '" --instance-pipe-prefix "' + $prefix +
            '" --registration-file "' + $RegistrationPath +
            '" --instance-pipe-sddl "' + $InstancePipeSddl + '"')
}

function New-D4ShareCaptureScriptContent {
    # P2/P4: the share probe capture wrapper (run-live.cmd). R62-D: the
    # task's default working directory is NOT assumed - the script pins its
    # own directory (cd /d %~dp0) and every argument is an ABSOLUTE path
    # under the fixed experiment contract; the Python home is an explicit
    # configuration slot (never guessed). The guard receives
    # --experiment-config (its launch-pinning contract consumer). Pure
    # function.
    param([hashtable]$Contract, [string]$ShareArgs, [string]$PythonHome)
    if (-not $PythonHome -or -not $PythonHome.Trim()) {
        throw 'New-D4ShareCaptureScriptContent: explicit -PythonHome required (never guessed)'
    }
    if (-not $ShareArgs -or -not $ShareArgs.Trim()) {
        throw 'New-D4ShareCaptureScriptContent: the composed share args are required'
    }
    $root = [string]$Contract['ExperimentRoot']
    # PS 5.1 argument-mode trap: 'Join-Path $root [string]$Contract[Key]' in
    # a bare argument position does NOT bind the cast to the following
    # expression - it renders '[string]System.Collections.Hashtable[Key]'
    # into the joined path (empirically reproduced). Bind the value to a
    # variable first so the cast evaluates in EXPRESSION context.
    $runDirRel = [string]$Contract['GuardRunRel']
    $runDir = Join-Path $root $runDirRel
    $configPath = Join-Path $root 'experiment-config.json'
    # R13-3: the upstream port is EXPLICIT and pinned to the experiment
    # share port: the experiment has no upstream backend, so a dial would be a
    # visible self-loop that can never reach the production share backend
    # (8110) or the private chat2api (8000). Placed after --probe so the
    # reviewed "--port 9100 --probe" launch shape stays byte-identical.
    $upstreamPort = [int]$Contract['SharePort']
    return @"
@echo off
rem run-live.cmd - D4 experiment share probe capture wrapper. GENERATED
rem content: every path is ABSOLUTE under the fixed experiment root, and
rem the working directory is pinned here because the task scheduler's
rem default cwd is never assumed to be the experiment root.
cd /d "%~dp0"
set "PYTHONHOME=$PythonHome"
set "PYTHONDONTWRITEBYTECODE=1"
set "UM_SHARE_ARGS=$ShareArgs"
"$root\um-share-python.exe" "$root\share_guard.py" --run-dir "$runDir" --port 9100 --probe --upstream-port $upstreamPort --experiment-config "$configPath"
"@
}

function New-D4ShareTaskDefinitionXml {
    # P4: the share task's LEGAL definition document (R62-D): NO trigger
    # elements (on-demand only, /Run is the only start path),
    # AllowStartOnDemand=true, the COLLECTED share identity as the principal
    # (never a guessed SID), the wrapper as the ONLY Exec action with empty
    # arguments. Pure function: deterministic output, hash-verifiable.
    # -LogonType: 'S4U' (the blocked task-logon form, kept byte-identical)
    # or 'Password' (§93 password-managed form: the S4U wording is NOT
    # blindly replaced - the Password variant ALSO switches RunLevel to
    # LeastPrivilege, the only meaningful level for a non-admin share identity).
    # The declaration MUST say UTF-16: Register-ScheduledTask passes the
    # .NET string (in-memory UTF-16) to the Task Scheduler COM parser, whose
    # MSXML loadXML semantics only accept UTF-16/UC-2 input - a UTF-8
    # declaration there is rejected with "无法切换编码" (cannot switch
    # encoding) at the declaration end; the real T0 run d4x-20260911-01 hit
    # exactly that. Same shape as New-BrokerTaskDefinitionXml (UTF-16,
    # registered successfully) and WriteXml's UTF-16LE+BOM file encoding.
    param([hashtable]$Contract, [hashtable]$Config, [string]$CommandPath,
          [string]$LogonType = 'S4U')
    if (-not $CommandPath) {
        throw 'New-D4ShareTaskDefinitionXml: the wrapper command path is required'
    }
    if ($LogonType -notin 'S4U', 'Password') {
        throw ("New-D4ShareTaskDefinitionXml: logon type must be 'S4U' or 'Password', got: " + $LogonType)
    }
    $principalSid = [string]$Config['share_sid']
    if (-not $principalSid) {
        throw 'New-D4ShareTaskDefinitionXml: the collected share SID is required (never guessed)'
    }
    $logonElement = if ($LogonType -eq 'Password') { 'Password' } else { 'S4U' }
    $runLevelElement = if ($LogonType -eq 'Password') { 'LeastPrivilege' } else { 'HighestAvailable' }
    $def = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>UM share D4 experiment probe capture (generated by execute-d4-experiment; bound to the experiment root)</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>__PRINCIPAL_SID__</UserId>
      <LogonType>__LOGON_TYPE__</LogonType>
      <RunLevel>__RUN_LEVEL__</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>__COMMAND__</Command>
      <Arguments></Arguments>
    </Exec>
  </Actions>
</Task>
'@
    $def = $def.Replace('__PRINCIPAL_SID__', $principalSid)
    $def = $def.Replace('__LOGON_TYPE__', $logonElement)
    $def = $def.Replace('__RUN_LEVEL__', $runLevelElement)
    $def = $def.Replace('__COMMAND__', $CommandPath)
    return $def
}

function New-D4SuggestedRunId {
    $sha = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 6
        $sha.GetBytes($bytes)
        return (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss') + '-' +
            ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-D4StageStatePath {
    # Stage evidence binds the FULL package hash + THE run id (R62-A).
    param([hashtable]$Contract, [string]$PackageHash, [string]$RunId)
    [void](Test-D4RunIdFormat $RunId)
    $rel = $Contract['StateDirRel'] + '\stage-state-' +
           $PackageHash.Substring(0, 8) + '-' + $RunId + '.json'
    return (Join-Path $Contract['ExperimentRoot'] $rel)
}

function Get-D4IntentLedgerPath {
    # The intent ledger path is generated ONLY here (R62-A/B): same binding
    # rules as the stage state (package hash prefix + THE approved run id).
    param([hashtable]$Contract, [string]$PackageHash, [string]$RunId)
    [void](Test-D4RunIdFormat $RunId)
    $rel = $Contract['StateDirRel'] + '\intent-' +
           $PackageHash.Substring(0, 8) + '-' + $RunId + '.json'
    return (Join-Path $Contract['ExperimentRoot'] $rel)
}

function Read-D4StageState {
    # R62-A: the ONLY reader. Validates the persisted STRUCTURE and binds
    # package hash + run id; a damaged/foreign state refuses, never
    # reinterpreted.
    param([string]$StatePath, [string]$PackageHash, [string]$RunId,
          [hashtable]$FileBoundary)
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($StatePath))) {
        return $null
    }
    $raw = Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($StatePath)
    $state = $null
    try {
        $state = $raw | ConvertFrom-Json
    } catch {
        throw ("execute-d4-experiment: stage state corrupt: " + $_.Exception.Message)
    }
    if ($null -eq $state -or $null -eq $state.PSObject.Properties['closure_hash'] -or
            $null -eq $state.PSObject.Properties['run_id'] -or
            $null -eq $state.PSObject.Properties['stages']) {
        throw ("execute-d4-experiment: stage state structure invalid (expected closure_hash/run_id/stages): " + $StatePath)
    }
    if ([string]$state.closure_hash -ne $PackageHash) {
        throw ("execute-d4-experiment: stage state belongs to a DIFFERENT package (recorded " +
               [string]$state.closure_hash + ", current " + $PackageHash +
               "); old PASS markers are never reused across versions")
    }
    if ([string]$state.run_id -ne $RunId) {
        throw ("execute-d4-experiment: stage state belongs to run " + [string]$state.run_id +
               ", not run " + $RunId)
    }
    $stages = @{}
    foreach ($prop in $state.stages.PSObject.Properties) {
        $entry = $prop.Value
        if ($null -eq $entry -or -not $entry.PSObject.Properties['verdict']) {
            throw ("execute-d4-experiment: stage entry invalid: " + $prop.Name)
        }
        if ([string]$entry.verdict -notin 'pass', 'fail', 'blocked') {
            throw ("execute-d4-experiment: stage verdict invalid: " + $prop.Name)
        }
        $stages[$prop.Name] = @{ verdict = [string]$entry.verdict;
                                 detail = [string]$entry.detail; at = [string]$entry.at }
    }
    return @{ PackageHash = [string]$state.closure_hash; RunId = [string]$state.run_id; Stages = $stages }
}

function Save-D4StageState {
    # R62-A: the ONLY writer. The fresh-record branch starts from an EMPTY
    # stage map (never from a hashtable's Count/Keys properties); an
    # existing record is re-validated first. Re-recording ANY verdict for
    # the same stage+run refuses - a retry is a new run id.
    param([string]$StatePath, [string]$PackageHash, [string]$RunId,
          [string]$Stage, [string]$Verdict, [string]$Detail,
          [hashtable]$FileBoundary)
    [void](Test-D4RunIdFormat $RunId)
    if ([string]$Verdict -notin 'pass', 'fail', 'blocked') {
        throw ("stage verdict must be pass/fail/blocked: " + $Verdict)
    }
    $existing = Read-D4StageState -StatePath $StatePath -PackageHash $PackageHash -RunId $RunId -FileBoundary $FileBoundary
    if ($null -ne $existing -and $existing['Stages'].ContainsKey($Stage)) {
        throw ("execute-d4-experiment: stage " + $Stage + " already recorded for run " + $RunId +
               " (verdict " + $existing['Stages'][$Stage].verdict + "); a re-run requires a new run id")
    }
    $stages = if ($null -ne $existing) { $existing['Stages'] } else { @{} }
    $stages[$Stage] = @{ verdict = [string]$Verdict; detail = [string]$Detail;
                         at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') }
    $out = @{ closure_hash = [string]$PackageHash; run_id = [string]$RunId; stages = $stages }
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($StatePath, ($out | ConvertTo-Json -Depth 6))
    return $out
}

function Test-D4StageAdvance {
    # R62-A: disorder refusal via the SAME validated reader. A stage may
    # start only when every PRIOR stage is recorded 'pass' for THIS package
    # hash + run id; a stage already recorded (any verdict) refuses.
    param([string]$StatePath, [string]$PackageHash, [string]$RunId,
          [string]$Stage, [hashtable]$FileBoundary)
    $order = @('S0', 'S1', 'E1', 'E2', 'E3', 'E4')
    $idx = $order.IndexOf($Stage)
    if ($idx -lt 0) { return $true }
    $state = Read-D4StageState -StatePath $StatePath -PackageHash $PackageHash -RunId $RunId -FileBoundary $FileBoundary
    if ($null -eq $state) {
        if ($idx -gt 0) {
            throw ("execute-d4-experiment: stage " + $Stage + " requires prior stage evidence, but no stage state exists for run " + $RunId)
        }
        return $true
    }
    if ($state['Stages'].ContainsKey($Stage)) {
        throw ("execute-d4-experiment: stage " + $Stage + " already recorded for run " + $RunId +
               " (verdict " + $state['Stages'][$Stage].verdict + "); refusing to re-run")
    }
    for ($k = 0; $k -lt $idx; $k++) {
        $prev = $state['Stages'][$order[$k]]
        if ($null -eq $prev -or $prev['verdict'] -ne 'pass') {
            throw ("execute-d4-experiment: stage disorder refused - " + $order[$k] +
                   " is not recorded as pass for run " + $RunId + "; " + $Stage +
                   " may not start (failure never advances to the next stage)")
        }
    }
    return $true
}

function Test-D4NamespaceOwnedTask {
    # §95.5: a share task PRE-registered by THIS run's bootstrap (T0) is part
    # of this run's namespace ONLY when it passes the FULL reviewed definition
    # shape (single Exec + exact command + empty arguments + no working dir +
    # exact principal SID + Password + LeastPrivilege + no automatic trigger)
    # - the SAME verifier Confirm-D4TaskIdentity uses, never a weaker
    # SID/logon-only subset. Any other same-name task stays a foreign
    # occupant.
    param([hashtable]$TaskSpec, [string]$Trigger, [scriptblock]$SchtasksRunner)
    $identity = Confirm-D4TaskIdentity -Task $TaskSpec -Trigger $Trigger `
        -SchtasksRunner $SchtasksRunner -LogonType ([string]$TaskSpec['LogonType'])
    return ($identity.State -eq 'owned')
}

function Test-D4NamespaceFree {
    # R62-B: THREE-STATE inventory (free / occupied / unconfirmed). A query
    # failure is NEVER absence. Checked: tasks, port listener, root content
    # (unknown entries occupy), runtime sources. Pipe names are probed at
    # E1 by the actual listener creation (an explicit not-ready fact).
    # -OwnShareTask (optional, from the run's bootstrap record) exempts THIS
    # run's own pre-registered share task from occupancy (SID+logon proof).
    param([hashtable]$Inventory, [hashtable]$Contract,
          [scriptblock]$SchtasksRunner, [scriptblock]$NetStatRunner,
          [hashtable]$FileBoundary, [hashtable]$OwnShareTask = $null)
    $details = @()
    $verdict = 'free'
    foreach ($t in $inventory['Tasks']) {
        $q = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
            'schtasks', '/Query', '/TN', $t.Name)
        $text = (@($q.Output) -join "`n")
        if ($q.ExitCode -eq 0) {
            $own = $false
            if ($null -ne $OwnShareTask -and $t.Name -eq [string]$OwnShareTask['Name']) {
                $own = Test-D4NamespaceOwnedTask -TaskSpec $OwnShareTask `
                    -Trigger ([string]$OwnShareTask['Trigger']) `
                    -SchtasksRunner $SchtasksRunner
            }
            if (-not $own) {
                $details += ("task present: " + $t.Name)
                $verdict = 'occupied'
            }
        } elseif (-not ($text -match 'cannot find|找不到')) {
            $details += ("task query FAILED (not provably absent): " + $t.Name + " (exit " + $q.ExitCode + ")")
            if ($verdict -ne 'occupied') { $verdict = 'unconfirmed' }
        }
    }
    if ($NetStatRunner) {
        $net = Invoke-CommandRunner -Runner $NetStatRunner -ArgumentList @(
            'netstat', '-ano', '-p', 'TCP')
        if ($net.ExitCode -ne 0) {
            $details += ("port check failed (exit " + $net.ExitCode + "); 9100 state UNCONFIRMED")
            if ($verdict -eq 'free') { $verdict = 'unconfirmed' }
        } else {
            $listen = @($net.Output | Where-Object { $_ -match ':\s*9100\s' -and $_ -match 'LISTENING' })
            if ($listen.Count -gt 0) {
                $details += "port 9100 has a listener"
                $verdict = 'occupied'
            }
        }
    } else {
        $details += "no netstat collector supplied; port state UNCONFIRMED"
        if ($verdict -eq 'free') { $verdict = 'unconfirmed' }
    }
    # ROOT content scan: anything under the root that is NOT a planned
    # directory occupies the namespace (unknown takeover - never cleared).
    # A List failure is UNCONFIRMED (an access-denied read is never absence).
    $plannedDirs = @{}
    foreach ($d in $inventory['Dirs']) { $plannedDirs[[string]$d] = $true }
    try {
        $rootItems = @(Invoke-D4File -Boundary $FileBoundary -Operation 'List' -Arguments @($inventory['Root']))
        foreach ($item in $rootItems) {
            if (-not $plannedDirs.ContainsKey([string]$item)) {
                $details += ("experiment root already contains unknown content: " + $item)
                $verdict = 'occupied'
            }
        }
    } catch {
        $details += ("root content check FAILED (unconfirmed): " + $_.Exception.Message)
        if ($verdict -eq 'free') { $verdict = 'unconfirmed' }
    }
    foreach ($rf in @($inventory['Files']) | Where-Object { $_.Kind -eq 'runtime-copy' }) {
        if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($rf.Source))) {
            $details += ("runtime source missing: " + $rf.Source)
            if ($verdict -eq 'free') { $verdict = 'unconfirmed' }
        }
    }
    # THIS run's own evidence records (intent ledger + stage state, bound to
    # the package hash + run id) are PLANNED ownership - a previous stage's
    # record inside the evidence dir is never foreign takeover (R62-A: the
    # S0->S1 chain reads/writes the SAME state file).
    $ownEvidence = @{}
    foreach ($f in $inventory['Files']) {
        if ($f.Kind -in 'intent-ledger', 'stage-state') {
            $ownEvidence[[string]$f.Path] = $true
        }
    }
    foreach ($d in $inventory['Dirs']) {
        try {
            $items = @(Invoke-D4File -Boundary $FileBoundary -Operation 'List' -Arguments @($d))
        } catch {
            $details += ("namespace directory check FAILED (unconfirmed): " + $d)
            if ($verdict -eq 'free') { $verdict = 'unconfirmed' }
            continue
        }
        $foreign = @($items | Where-Object { -not $ownEvidence.ContainsKey([string]$_) })
        if ($foreign.Count -gt 0) {
            foreach ($i in $foreign) { $details += ("experiment namespace already contains: " + $i) }
            $verdict = 'occupied'
        }
    }
    return @{ Verdict = $verdict; Details = $details }
}

function New-D4IntentLedger {
    # R62-B: persisted BEFORE any object is created; records every planned
    # object (path/kind/expected hash/producer). After each successful
    # creation the entry gains the ACTUAL hash; on failure the ledger lists
    # every created-but-unresolved object (bounded recovery basis).
    param([hashtable]$Inventory, [string]$LedgerPath, [hashtable]$FileBoundary)
    $entries = @()
    foreach ($d in $inventory['Dirs']) {
        $entries += @{ path = $d; kind = 'dir'; status = 'planned' }
    }
    foreach ($f in $inventory['Files']) {
        if ($f.Kind -in 'intent-ledger', 'stage-state') { continue }
        $entries += @{ path = $f.Path; kind = $f.Kind; sha256_expected = $f.Sha256;
                       source = $f.Source; producer = $f.Producer; status = 'planned' }
    }
    foreach ($t in $inventory['Tasks']) {
        $entries += @{ path = ('task:' + $t.Name); kind = 'task'; identity = $t.Identity;
                       trigger = $t.Trigger; command = $t.Command; status = 'planned' }
    }
    # R67-B: the limit rules are CREATED experiment objects - their
    # ownership travels in the SAME ledger (recorded BEFORE creation by
    # Invoke-D4PrepareLimits), so cleanup can identity-check and remove
    # them exactly like files and tasks.
    foreach ($r in @($inventory['RuleSpecs'])) {
        $entries += @{ path = ('rule:' + [string]$r.id); kind = 'rule';
                       layer = [string]$r.layer; action = [string]$r.action;
                       app_path = [string]$r.app_path; status = 'planned' }
    }
    $ledger = @{ run_id = $inventory['RunId']; package_hash = $inventory['PackageHash'];
                 entries = $entries }
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($LedgerPath, ($ledger | ConvertTo-Json -Depth 6))
    return $ledger
}

function Update-D4IntentLedger {
    # Records one object's ACTUAL post-creation state (atomic rewrite).
    # R80: the ledger is accepted in EITHER runtime form - the S1 builder's
    # lowercase contract AND the cleanup-side PascalCase load. A phase that
    # loaded via Read-D4IntentLedger (PascalCase) used to hit the lowercase
    # 'entries' lookup as $null and silently rewrite the ledger with an
    # EMPTY entries list - destroying the run's ownership record mid-flight.
    # R83: -Retention marks the per-object reason for the 'retained' legal
    # terminal state (a dynamic artifact deliberately kept - never deleted,
    # never claimed on re-computed hashes, never stamped as a frozen/final
    # writer state).
    param([hashtable]$Ledger, [string]$LedgerPath, [string]$Path,
          [string]$Status, [string]$Sha256,
          [string]$Retention,
          [hashtable]$FileBoundary)
    $source = if ($null -ne $ledger['entries']) { @($ledger['entries']) }
              elseif ($null -ne $ledger['Entries']) { @($ledger['Entries']) }
              else { throw ('intent ledger has no entries list: ' + $LedgerPath) }
    $entries = @()
    foreach ($e in $source) {
        if ([string]$e['path'] -eq $Path) {
            $e['status'] = [string]$Status
            if ($Sha256) { $e['sha256_actual'] = [string]$Sha256 }
            if ($Retention) { $e['retained_reason'] = [string]$Retention }
        }
        $entries += $e
    }
    $ledger['entries'] = $entries
    if ($null -ne $ledger['Entries']) { $ledger['Entries'] = $entries }
    # R67-C: the persisted form is ALWAYS the canonical lowercase contract
    # (run_id/package_hash/entries) - the CLEANUP-side ledger is loaded via
    # Read-D4IntentLedger into the PascalCase runtime form (RunId/Entries);
    # writing THAT form back would corrupt the read contract for the next
    # load (the repeated-cleanup idempotency caught it).
    $persisted = @{
        run_id = if ($ledger['run_id']) { [string]$ledger['run_id'] } else { [string]$ledger['RunId'] }
        package_hash = if ($ledger['package_hash']) { [string]$ledger['package_hash'] } else { [string]$ledger['PackageHash'] }
        entries = $entries
    }
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($LedgerPath, ($persisted | ConvertTo-Json -Depth 6))
    return $ledger
}

function Add-D4IntentLedgerEntry {
    # R78-C: records a created object THIS run's inventory never planned
    # (an install artifact the manifest proves) - APPENDS one entry and
    # persists the canonical lowercase form. A path already in the ledger
    # refuses: post-creation state changes go through Update-D4IntentLedger
    # (a silent append over an owned record would bypass its semantics).
    param([hashtable]$Ledger, [string]$LedgerPath, [string]$Path,
          [string]$Kind, [string]$Sha256, [hashtable]$FileBoundary)
    if ($null -eq $Ledger['entries']) {
        throw ('intent ledger has no entries list: ' + $LedgerPath)
    }
    foreach ($e in @($Ledger['entries'])) {
        if ([string]$e['path'] -eq $Path) {
            throw ('intent ledger already owns a record for ' + $Path)
        }
    }
    $entry = @{ path = [string]$Path; kind = [string]$Kind; status = 'created' }
    if ($Sha256) { $entry['sha256_actual'] = [string]$Sha256 }
    $entries = @($Ledger['entries']) + @($entry)
    $Ledger['entries'] = $entries
    $persisted = @{
        run_id = if ($Ledger['run_id']) { [string]$Ledger['run_id'] } else { [string]$Ledger['RunId'] }
        package_hash = if ($Ledger['package_hash']) { [string]$Ledger['package_hash'] } else { [string]$Ledger['PackageHash'] }
        entries = $entries
    }
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($LedgerPath, ($persisted | ConvertTo-Json -Depth 6))
    return $Ledger
}

function Import-D4InstallManifestOwnership {
    # R78-C: the install's OWN manifest decides what THIS run created - the
    # SINGLE import contract shared by the success path and the S1 abort
    # path. Entries classified 'created' with a non-empty sha256 are
    # imported into the intent ledger WITH their actual hash (the cleanup
    # deletion gate then works): an entry this run already planned is
    # UPDATED; an artifact outside the S1 inventory is ADDED as an
    # owned-artifact record (a silent drop here would leave a real install
    # artifact unowned and make the same-run cleanup unfinishable).
    # 'shared'/'planned' pre-existing artifacts stay OUT of this run's
    # owned set (the install's own manifest owns their recovery).
    param([string]$ManifestPath, [string]$ResolvedRoot,
          [hashtable]$Ledger, [string]$LedgerPath, [hashtable]$FileBoundary)
    $installManifest = (Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($ManifestPath)) |
        ConvertFrom-Json
    foreach ($m in @($installManifest.modules)) {
        $rel = [string]$m.file
        $cls = [string]$m.classification
        $sha = [string]$m.sha256
        if ($cls -ne 'created' -or -not $sha -or $sha -notmatch '^[0-9a-f]{64}$') {
            continue   # pre-existing/shared/runtime artifact - never claimed
        }
        $targetPath = Join-Path $ResolvedRoot $rel
        $known = @($Ledger['entries'] | Where-Object { [string]$_['path'] -eq $targetPath })
        if ($known.Count -gt 0) {
            $Ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $targetPath -Status 'created' -Sha256 $sha -FileBoundary $FileBoundary
        } else {
            $Ledger = Add-D4IntentLedgerEntry -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $targetPath -Kind 'owned-artifact' -Sha256 $sha -FileBoundary $FileBoundary
            Write-Log ("install artifact imported as owned (not pre-planned): " + $rel)
        }
    }
    return $Ledger
}

function Save-D4DynamicRetention {
    # R83 retention contract (§83.4): the DYNAMIC artifacts (projection /
    # isolated store / install manifest) legitimately change after S1
    # recorded their creation hash. OFFLINE their final state is NOT provable
    # the way the deletion gate requires - a PLANNED path is not an ownership
    # fact (a stable foreign file may sit there), and two equal hash samples
    # do not prove the writer stopped (a still-alive but silent writer reads
    # identically). Granting a fresh deletion right on that evidence would
    # let the cleanup delete foreign/unknown bytes. The bounded behaviour
    # therefore never claims and never deletes:
    #   * the entry is marked with the LEGAL terminal state 'retained' plus a
    #     per-object reason; the recorded status/hash/ownership are NEVER
    #     overwritten (no planned->created upgrade, no current-hash adoption -
    #     recomputing the hash of an existing file would claim a foreign
    #     object as this run's product);
    #   * nothing here asserts "writer stopped" - service-stop is a SEPARATE
    #     fact the cleanup verifies against task/process/listener/rule
    #     evidence, reported distinctly from the kept files.
    # Cleanup keeps retained objects (never deletes them) and reports them.
    param([string]$LedgerPath, [string]$PackageHash, [string]$RunId,
          [string]$ExperimentRoot, [string[]]$RelativePaths,
          [string]$Retention, [hashtable]$FileBoundary)
    $ledger = Read-D4IntentLedger -LedgerPath $LedgerPath -PackageHash $PackageHash `
        -RunId $RunId -FileBoundary $FileBoundary
    if ($null -eq $ledger) { return $null }
    foreach ($rel in @($RelativePaths)) {
        $p = Join-Path $ExperimentRoot $rel
        # A path with no ledger record is never claimed (no entry, no action).
        $owned = @($ledger['entries'] | Where-Object { [string]$_['path'] -eq $p })[0]
        if ($null -eq $owned) { continue }
        if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($p))) {
            continue   # nothing at this path to keep yet
        }
        # Retention terminal: keep the file, record the reason per object.
        # The status moves to the legal 'retained' terminal; the recorded
        # ownership/hash stay as they are (the hash is never replaced by the
        # current bytes).
        $ledger = Update-D4IntentLedger -Ledger $ledger -LedgerPath $LedgerPath -Path $p `
            -Status 'retained' -Retention $Retention -FileBoundary $FileBoundary
    }
    return $ledger
}

function Get-D4LedgerUnresolved {
    # Bounded recovery: after ANY failure the ledger explains every object.
    # 'retained' is a LEGAL terminal (R83 deliberate keep) - never reported
    # as an unresolved/open obligation.
    param([hashtable]$Ledger)
    $unresolved = @()
    foreach ($e in $ledger['entries']) {
        if ($e.status -notin 'planned', 'removed', 'failed-clean', 'retained') {
            $unresolved += ($e.path + ' (' + $e.status + ')')
        }
    }
    return $unresolved
}

function Read-D4IntentLedger {
    # Cleanup-side load of THIS run's intent ledger (R62-B): the ONLY disk
    # load path. The record must carry run_id + package_hash bound to THIS
    # run and a legal entries list; a foreign/corrupt ledger refuses (never
    # reinterpreted). Absent file -> $null (nothing was ever planned).
    param([string]$LedgerPath, [string]$PackageHash, [string]$RunId,
          [hashtable]$FileBoundary)
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($LedgerPath))) {
        return $null
    }
    $raw = Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($LedgerPath)
    try {
        $obj = $raw | ConvertFrom-Json
    } catch {
        throw ("execute-d4-experiment: intent ledger corrupt: " + $_.Exception.Message)
    }
    if ($null -eq $obj -or $null -eq $obj.PSObject.Properties['run_id'] -or
            $null -eq $obj.PSObject.Properties['package_hash'] -or
            $null -eq $obj.PSObject.Properties['entries']) {
        throw ("execute-d4-experiment: intent ledger structure invalid (expected run_id/package_hash/entries): " + $LedgerPath)
    }
    if ([string]$obj.run_id -ne $RunId) {
        throw ("execute-d4-experiment: intent ledger belongs to run " + [string]$obj.run_id +
               ", not run " + $RunId)
    }
    if ([string]$obj.package_hash -ne $PackageHash) {
        throw ("execute-d4-experiment: intent ledger belongs to package " + [string]$obj.package_hash +
               ", not the current package " + $PackageHash)
    }
    $entries = @()
    foreach ($e in @($obj.entries)) {
        $entry = @{}
        foreach ($p in $e.PSObject.Properties) { $entry[$p.Name] = $p.Value }
        if (-not [string]$entry['path'] -or -not [string]$entry['kind'] -or -not [string]$entry['status']) {
            throw ("execute-d4-experiment: intent ledger entry invalid: " + $LedgerPath)
        }
        $entries += $entry
    }
    return @{ RunId = [string]$obj.run_id; PackageHash = [string]$obj.package_hash;
              Entries = $entries }
}

function Test-D4RuleFactsMatch {
    # R67-A: the STRONG semantic verdict of ONE limit rule. The runner emits
    # STRUCTURED FACTS read from the live filter object (layer GUID, action
    # code, condition count, the app-id blob byte-match against the spec's
    # own image path, and the permit scope values); this pure function
    # compares them with the EXACT spec. A same-key rule with a different
    # action, layer, app-id or scope is a MISMATCH - never adopted.
    param([hashtable]$Spec, [string]$FactsText, [hashtable]$Contract)
    $layerGuid = [string]$script:D4WfpLayerGuids[[string]$spec['layer']]
    $want = $script:D4WfpActionCode[[string]$spec['action']]
    $m = [regex]::Match($FactsText,
        'layer=([0-9a-fA-F-]+)\s+action=(0x[0-9a-fA-F]+)\s+conds=(\d+)\s+appid=(MATCH|MISMATCH|NONE)\s+addr=([0-9]+|NA)\s+port=(\d+|NA)')
    if (-not $m.Success) {
        return @{ Ok = $false; Reason = 'facts line unparsable: ' + $FactsText.Trim() }
    }
    $factsLayer = $m.Groups[1].Value.ToLowerInvariant()
    $factsAction = $m.Groups[2].Value.ToLowerInvariant()
    $factsConds = [int]$m.Groups[3].Value
    $factsAppId = $m.Groups[4].Value
    $factsAddr = $m.Groups[5].Value
    $factsPort = $m.Groups[6].Value
    $expectConds = 1; $expectAddr = 'NA'; $expectPort = 'NA'
    if ([string]$spec['action'] -eq 'permit') {
        $expectConds = 3
        $expectAddr = '2130706433'   # 127.0.0.1 as a host-order uint32
        $expectPort = [string]$Contract['SharePort']
    }
    if ($factsLayer -ne $layerGuid.ToLowerInvariant()) {
        return @{ Ok = $false; Reason = ('layer mismatch (got ' + $factsLayer + ', want ' + $layerGuid + ')') }
    }
    if ($factsAction -ne ('0x{0:x4}' -f $want)) {
        return @{ Ok = $false; Reason = ('action mismatch (got ' + $factsAction + ', want 0x' + ('{0:x4}' -f $want) + ')') }
    }
    if ($factsAppId -ne 'MATCH') {
        return @{ Ok = $false; Reason = ('app-id condition does not match the experiment image (' + $factsAppId + ')') }
    }
    if ($factsConds -ne $expectConds) {
        return @{ Ok = $false; Reason = ('condition count mismatch (got ' + $factsConds + ', want ' + $expectConds + '; a narrowed/widened rule never passes)') }
    }
    if ($factsAddr -ne $expectAddr -or $factsPort -ne $expectPort) {
        return @{ Ok = $false; Reason = ('scope mismatch (got addr=' + $factsAddr + ' port=' + $factsPort +
                                            ', want addr=' + $expectAddr + ' port=' + $expectPort + ')') }
    }
    return @{ Ok = $true; Reason = '' }
}

function Test-D4LimitsReady {
    # R67-A/E1 gate: EVERY configured rule spec must be PRESENT with facts
    # matching its specification. ABSENT / mismatch / query failure is an
    # explicit refusal (limits not ready or wrong) - zero starts.
    param([hashtable]$Inventory, [scriptblock]$RulesRunner, [hashtable]$Contract)
    $details = @()
    foreach ($rawSpec in @($inventory['RuleSpecs'])) {
        $spec = ConvertTo-D4RuleSpec -Raw $rawSpec
        $id = [string]$spec['id']
        $q = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            'limits', 'verify', $id)
        $text = (@($q.Output) -join "`n")
        if ($q.ExitCode -eq 2) {
            $details += ("limit query FAILED (unconfirmed): " + $id + ": " + $text.Trim())
            continue
        }
        if ($q.ExitCode -eq 1) {
            $details += ("limit rule ABSENT (limits not ready): " + $id)
            continue
        }
        $verdict = Test-D4RuleFactsMatch -Spec $spec -FactsText $text -Contract $Contract
        if (-not $verdict['Ok']) {
            $details += ("limit rule MISMATCH: " + $id + " (" + $verdict['Reason'] + ")")
            continue
        }
    }
    return @{ Ready = ($details.Count -eq 0); Details = $details }
}

function Invoke-D4PrepareSharePythonHomeAccess {
    # E1 (W2): the share task runs share_guard.py AS the experiment share
    # account. Production grants ITS share account (um_share) an explicit
    # (OI)(CI)RX ACE on the python_home so the share process can load the
    # stdlib; the D4 experiment share account needs the SAME minimal grant,
    # or the share task dies in <1s ("No module named 'encodings'"). This
    # grants ONLY that SID's ACE on the python_home (icacls /grant:r
    # replaces exactly the named principal - every other ACE stays intact;
    # never /inheritance:r on a user-owned directory). The grant is
    # ledger-gated (acl:pythonhome owned by THIS run), verified by reading
    # the ACL back, and reversed by Invoke-D4RemoveSharePythonHomeAccess
    # in cleanup.
    param([string]$PythonHome, [string]$ShareSid, [scriptblock]$IcaclsRunner,
          [hashtable]$Ledger, [string]$LedgerPath, [hashtable]$FileBoundary)
    if (-not $PythonHome) { throw 'python home is required for the share account grant' }
    if (-not $ShareSid) { throw 'share SID is required for the python home grant' }
    # A missing python_home fails CLOSED at the native layer: icacls /grant:r
    # against a nonexistent path returns nonzero and the grant throws - an ACE
    # is never fabricated on a path that does not exist. (An offline fake
    # directory trivially passes grant+verify; the real S1 path only ever runs
    # against the collected, already-frozen python_home.)
    $ledgerKey = 'acl:pythonhome'
    # The ledger records the fact BEFORE the change (the ownership gate for
    # the cleanup-side /remove is THIS run's created record).
    if ($null -ne $Ledger) {
        $existingGrant = @($Ledger['entries'] | Where-Object {
            [string]$_['path'] -eq $ledgerKey })[0]
        if ($null -eq $existingGrant) {
            $ledger = Add-D4IntentLedgerEntry -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $ledgerKey -Kind 'acl' -FileBoundary $FileBoundary
        } else {
            $ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $ledgerKey -Status 'created' -FileBoundary $FileBoundary
        }
    }
    # Grant ONLY the share account's read+execute set (the exact production
    # pattern: (OI)(CI)RX on the directory, inherited by the stdlib files).
    $grant = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $PythonHome, '/grant:r', ('*' + $ShareSid + ':(OI)(CI)RX'))
    if ($grant.ExitCode -ne 0) {
        throw ("share python_home grant failed (exit " + $grant.ExitCode + "): " + (@($grant.Output) -join ' '))
    }
    # Read the ACL back and verify the share SID now resolves to RX.
    $probe = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $PythonHome)
    $verdict = Test-PythonHomeAceForShare -Output @($probe.Output) `
        -ShareSid $ShareSid -PythonHome $PythonHome
    if (-not $verdict['Ok']) {
        throw ("share python_home grant VERIFY failed: " + [string]$verdict['Reason'] +
               "; ACL state kept for review, the share account still cannot read the runtime")
    }
    return @{ Ledger = $ledger }
}

function Invoke-D4RemoveSharePythonHomeAccess {
    # E1/W2 cleanup mirror: remove the python_home ACE this run granted to
    # the share account (icacls /remove by SID removes exactly that
    # principal), verify it is gone, and advance the ledger. A foreign ACE
    # for the same principal (not created by this run) is never removed.
    param([string]$PythonHome, [string]$ShareSid, [scriptblock]$IcaclsRunner,
          [hashtable]$Ledger, [string]$LedgerPath, [hashtable]$FileBoundary)
    if (-not $PythonHome -or -not $ShareSid) {
        throw 'python home and share SID are required to remove the share grant'
    }
    $ledgerKey = 'acl:pythonhome'
    $owned = $false
    if ($null -ne $Ledger) {
        foreach ($e in $Ledger['entries']) {
            if ([string]$e.path -eq $ledgerKey -and [string]$e.status -eq 'created') {
                $owned = $true
            }
        }
    }
    if (-not $owned) {
        # No THIS-run creation fact: never touch the ACL (same policy as the
        # limit-rule removal - a foreign grant is kept, never removed). The
        # ledger passes through UNCHANGED - the cleanup's own ledger flow
        # depends on it (a null here would corrupt the final verdict).
        return @{ Ledger = $Ledger; KeptForeign = @('acl:pythonhome (no THIS-run grant recorded; kept, not removed)') }
    }
    $probe = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $PythonHome)
    $verdict = Test-PythonHomeAceForShare -Output @($probe.Output) `
        -ShareSid $ShareSid -PythonHome $PythonHome
    if (-not $verdict['Ok'] -and [string]$verdict['State'] -eq 'absent') {
        # Already absent - the recovery already ran; advance ledger.
        if ($null -ne $Ledger) {
            $ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $ledgerKey -Status 'removed' -FileBoundary $FileBoundary
        }
        Write-Log ("share python_home ACE already ABSENT (idempotent); ledger advanced: " + $PythonHome)
        return @{ Ledger = $ledger; KeptForeign = @() }
    }
    if (-not $verdict['Ok']) {
        throw ("share python_home ACE state UNCONFIRMED before remove (exit/query); nothing removed: " + [string]$verdict['Reason'])
    }
    $rm = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $PythonHome, '/remove', ('*' + $ShareSid))
    if ($rm.ExitCode -ne 0) {
        throw ("share python_home ACE remove failed (exit " + $rm.ExitCode + "); nothing removed, evidence kept")
    }
    $probe2 = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $PythonHome)
    $v2 = Test-PythonHomeAceForShare -Output @($probe2.Output) `
        -ShareSid $ShareSid -PythonHome $PythonHome
    if (-not $v2['Ok'] -and [string]$v2['State'] -ne 'absent') {
        throw ("share python_home ACE still present after /remove (unconfirmed); ledger NOT advanced")
    }
    if ($null -ne $Ledger) {
        $ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
            -Path $ledgerKey -Status 'removed' -FileBoundary $FileBoundary
    }
    Write-Log ("share python_home ACE removed (identity verified, ABSENT confirmed): " + $PythonHome)
    return @{ Ledger = $ledger; KeptForeign = @() }
}

function Test-PythonHomeAceForShare {
    # Pure verdict over icacls output for ONE directory: does the share SID
    # hold a read+execute ACE (RX / ReadAndExecute), or is it absent? The
    # principal is resolved canonically (SID literal or NTAccount->SID) the
    # SAME way the ACL matrix verifier normalizes - a name-vs-SID string
    # comparison would false-negative on this host (icacls prints account
    # names). Returns @{ Ok; State = 'present'|'absent'|'unconfirmed'; Reason }.
    # The colon split must happen AFTER the path prefix is stripped - the
    # python home itself contains a drive colon ('C:\...') which would
    # otherwise be mistaken for the principal/rights separator.
    param([string[]]$Output, [string]$ShareSid, [string]$PythonHome)
    if (-not $ShareSid) { return @{ Ok = $false; State = 'unconfirmed'; Reason = 'share SID missing' } }
    foreach ($line in @($Output)) {
        $t = [string]$line
        if ($t -match '^\s*已成功处理|^\s*Successfully processed|^\s*处理的文件') { continue }
        $t = $t.Trim()
        # icacls echoes the queried path as a header line - strip it exactly
        # when it leads the line (the ACEs follow on the same physical line).
        if ($t.StartsWith($PythonHome, [StringComparison]::OrdinalIgnoreCase)) {
            $t = $t.Substring($PythonHome.Length).Trim()
        }
        if (-not $t -or $t.Equals($PythonHome, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $idx = $t.IndexOf(':')
        if ($idx -le 0) { continue }
        $principalText = $t.Substring(0, $idx).Trim()
        if ($principalText.StartsWith('*')) { $principalText = $principalText.Substring(1) }
        $rights = $t.Substring($idx + 1)
        $resolved = Resolve-D4TaskPrincipalSid -PrincipalText $principalText
        if (-not $resolved.ok) { continue }
        if ([string]$resolved.sid -ne $ShareSid) { continue }
        $rightsUp = $rights.ToUpperInvariant()
        if ($rightsUp -match '\(RX\)|RX|READANDPERMISSION|READ') {
            return @{ Ok = $true; State = 'present'; Reason = ('share SID ' + $ShareSid + ' holds ' + $rights) }
        }
        return @{ Ok = $false; State = 'present'; Reason = ('share SID present WITHOUT read+execute rights: ' + $rights) }
    }
    return @{ Ok = $false; State = 'absent'; Reason = ('share SID ' + $ShareSid + ' has no ACE here (absent)') }
}

function Invoke-D4PrepareLimits {
    # R69-B: OWNERSHIP comes from THIS RUN's ledger fact, never from a
    # matching specification. The runner reports CREATED (we made it) or
    # ALREADY-PRESENT (it existed before this call); an ALREADY-PRESENT
    # rule is only accepted when THIS RUN's ledger already records the
    # rule as created (a genuine same-run reentry) - any pre-existing rule
    # without that record is FOREIGN and refuses takeover, even when its
    # facts are identical to the spec.
    param([hashtable]$Inventory, [scriptblock]$RulesRunner, [hashtable]$Contract,
          [hashtable]$Ledger, [string]$LedgerPath, [hashtable]$FileBoundary)
    foreach ($rawSpec in @($inventory['RuleSpecs'])) {
        $spec = ConvertTo-D4RuleSpec -Raw $rawSpec
        $id = [string]$spec['id']
        $ledgerKey = 'rule:' + $id
        $owned = $false
        if ($null -ne $Ledger) {
            foreach ($e in $Ledger['entries']) {
                if ([string]$e.path -eq $ledgerKey -and [string]$e.status -eq 'created') {
                    $owned = $true
                }
            }
        }
        $c = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            'limits', 'create', $id)
        $text = (@($c.Output) -join "`n")
        if ($c.ExitCode -eq 3) {
            throw ("limit rule " + $id + " already exists with a FOREIGN specification; refusing takeover: " + $text.Trim())
        }
        if ($c.ExitCode -ne 0) {
            throw ("limit rule create failed for " + $id + " (exit " + $c.ExitCode + "): " + $text.Trim())
        }
        if (($text -match 'ALREADY-PRESENT') -and (-not $owned)) {
            # Same spec is NOT ownership: the rule pre-dates this run and no
            # ledger fact says THIS run created it - never claimed, never
            # deleted by this run later.
            throw ("limit rule " + $id + " already existed BEFORE this run (same spec is not ownership); refusing takeover")
        }
        # Verify AFTER creation: the live filter must match the spec.
        $v = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            'limits', 'verify', $id)
        $vtext = (@($v.Output) -join "`n")
        if ($v.ExitCode -ne 0) {
            throw ("limit rule verify failed after create for " + $id + " (exit " + $v.ExitCode + "): " + $vtext.Trim())
        }
        $verdict = Test-D4RuleFactsMatch -Spec $spec -FactsText $vtext -Contract $Contract
        if (-not $verdict['Ok']) {
            throw ("limit rule created but its facts do not match the specification (" + $id + "): " + $verdict['Reason'])
        }
        if ($null -ne $Ledger) {
            $ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
                -Path $ledgerKey -Status 'created' -FileBoundary $FileBoundary
        }
        Write-Log ("limit rule created+verified: " + $id)
    }
    return @{ Ledger = $ledger }
}

function Invoke-D4RemoveLimits {
    # R69-B: the removal universe is THIS RUN's ledger facts - a rule entry
    # with status 'created' by THIS run. A spec without such a record
    # (pre-existing foreign, planned-only, already removed) is NEVER
    # touched. After the delete, the verify must return the EXPLICIT ABSENT
    # evidence (exit 1 + the ABSENT line for the id): exit 0 means the
    # filter is still live (refuse), exit 2 (query failure) is UNCONFIRMED
    # - the ledger is NOT advanced to removed in either case.
    param([hashtable]$Inventory, [scriptblock]$RulesRunner,
          [hashtable]$Ledger, [string]$LedgerPath, [hashtable]$FileBoundary)
    $keptForeign = @()
    $ownedRules = @{}
    if ($null -ne $Ledger) {
        foreach ($e in $Ledger['entries']) {
            if ([string]$e.kind -eq 'rule' -and [string]$e.status -eq 'created') {
                $ownedRules[[string]$e.path.Substring('rule:'.Length)] = $true
            }
        }
    }
    foreach ($rawSpec in @($inventory['RuleSpecs'])) {
        $spec = ConvertTo-D4RuleSpec -Raw $rawSpec
        $id = [string]$spec['id']
        if (-not $ownedRules.ContainsKey($id)) {
            # No THIS-run creation fact: not our object - kept, never touched.
            $keptForeign += ('rule:' + $id + ' (no ledger record of THIS run creating it; kept)')
            continue
        }
        $r = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            'limits', 'remove', $id)
        $text = (@($r.Output) -join "`n")
        if ($r.ExitCode -eq 3) {
            $keptForeign += ('rule:' + $id + ' (live filter does not match this run''s specification; kept)')
            continue
        }
        if ($r.ExitCode -ne 0) {
            throw ("limit rule remove failed for " + $id + " (exit " + $r.ExitCode + "); cleanup stops, evidence kept: " + $text.Trim())
        }
        # Explicit ABSENT evidence: exit 1 + the ABSENT line for THIS id.
        $c = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            'limits', 'verify', $id)
        $ctext = (@($c.Output) -join "`n")
        $absentLine = ($ctext -match ('(?m)^ABSENT\s+' + [regex]::Escape($id) + '\s*$'))
        if ($c.ExitCode -eq 1 -and $absentLine) {
            if ($null -ne $Ledger) {
                $ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
                    -Path ('rule:' + $id) -Status 'removed' -FileBoundary $FileBoundary
            }
            Write-Log ("limit rule removed (identity verified, ABSENT confirmed): " + $id)
        } elseif ($c.ExitCode -eq 0) {
            throw ("limit rule " + $id + " still PRESENT after remove (exit 0); cleanup stops, evidence kept")
        } else {
            # exit 2 or a missing verdict line: UNCONFIRMED - never removed.
            throw ("limit rule " + $id + " post-delete state UNCONFIRMED (exit " + $c.ExitCode + "); ledger NOT advanced; cleanup stops, evidence kept")
        }
    }
    return @{ Ledger = $ledger; KeptForeign = $keptForeign }
}

function Invoke-D4PrepareExperimentAcl {
    # R69-C/D: the ROOT + run\live DACL matrix (§60.5-S1, consumer-driven):
    # SYSTEM/admin full, the operator read-only, um_share(SID) RX on the
    # root (read+execute the image/scripts/config/instance key - the share
    # task runs as um_share and MUST read them) and a registration-write
    # set on run\live ONLY (RD,WD,AD,X). The broker dir (control secret)
    # stays protected by the install's OWN protected DACL. Grants are
    # SID-bound (never a name lookup) and the dir DACL is PROTECTED
    # (/inheritance:r) before the explicit grants - inherited/other ACEs
    # are removed, not merely overridden.
    # Recovery: /save backs the pre-change ACL into the evidence dir; the
    # state file is reconciled BEFORE the save (pending/failed refuse; a
    # prior applied entry with a kept backup refuses - never overwritten);
    # the pending state persists BEFORE the grant; a failed grant restores
    # (none) or records failed. ALL state writes are atomic through the
    # FileBoundary. Returns the updated ledger.
    param([string]$Root, [string]$RunDir, [hashtable]$Cfg,
          [scriptblock]$IcaclsRunner, [hashtable]$Ledger, [string]$LedgerPath,
          [hashtable]$FileBoundary, [string]$EvidenceDir)
    $operator = [string]$Cfg['operator_account']
    $shareSid = [string]$Cfg['share_sid']
    if (-not $shareSid) { throw 'experiment ACL matrix requires the collected share SID (never guessed)' }
    $statePath = Join-Path $EvidenceDir 'acl-recovery.json'
    $state = @{ dirs = @() }
    if (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($statePath)) {
        # R73-B: the prior record goes through the SAME shared contract -
        # shape, enum, target<->backup pairing, uniqueness; a corrupt prior
        # record refuses the prepare (never a silent empty state).
        $prior = ConvertFrom-D4AclRecoveryRecord -RawRecord (
            Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($statePath)) `
            -ExperimentRoot ([string]$Root)
        foreach ($p in @($prior['Dirs'])) {
            $state['dirs'] += @{ path = [string]$p['path']; backup_rel = [string]$p['backup_rel']; state = [string]$p['state'] }
        }
    }
    $targets = @(
        @{ Path = $Root;   Name = 'root';
           Ace = @('SYSTEM:(OI)(CI)F', 'BUILTIN\Administrators:(OI)(CI)F',
                   ($operator + ':(OI)(CI)RX'),
                   ('*' + $shareSid + ':(OI)(CI)RX'));
           Full = @('SYSTEM', 'Administrators'); ReadOnly = $operator;
           Limited = @{ ('*' + $shareSid) = @('RX') } },
        @{ Path = $RunDir; Name = 'run-live';
           Ace = @('SYSTEM:(OI)(CI)F', 'BUILTIN\Administrators:(OI)(CI)F',
                   ($operator + ':(OI)(CI)RX'),
                   ('*' + $shareSid + ':(OI)(CI)M')) }
    )
    $targets[1]['Full'] = @('SYSTEM', 'Administrators')
    $targets[1]['ReadOnly'] = $operator
    # §210 W2: icacls on this host only accepts SINGLE-symbol masks (comma
    # combos and AD/X symbols return exit 87); the run-live share grant is the
    # modify set and maps to the single legal code 'M'.
    $targets[1]['Limited'] = @{ ('*' + $shareSid) = @('M') }
    foreach ($d in $targets) {
        $backupRel = 'acl-backup-' + $d['Name'] + '.dat'
        $backupPath = Join-Path $EvidenceDir $backupRel
        # R69-C: reconcile the PREVIOUS state for THIS dir before any new
        # save - pending/failed = an unconverged recovery (refuse, never
        # overwrite the old backup); applied/none = consumable.
        $priorEntry = $null
        foreach ($s in @($state['dirs'])) {
            if ([string]$s['path'] -eq $d.Path) { $priorEntry = $s }
        }
        if ($null -ne $priorEntry -and [string]$priorEntry['state'] -in 'pending', 'failed') {
            throw ("experiment ACL for " + $d.Path + " is still " + [string]$priorEntry['state'] +
                   " from an earlier prepare; run the recovery before re-preparing")
        }
        if ($null -ne $priorEntry -and [string]$priorEntry['state'] -eq 'applied' -and
                (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($backupPath))) {
            throw ("experiment ACL for " + $d.Path + " still holds an applied-state backup (" +
                   $priorEntry['backup_rel'] + "); consume it via recovery/cleanup before re-preparing")
        }
        # Ownership first: the ledger records the ACL fact BEFORE the change.
        if ($Ledger -and -not (@($Ledger['entries']) | Where-Object { [string]$_['path'] -eq ('acl:' + $d['Name']) })) {
            $Ledger['entries'] = @($Ledger['entries']) + @(
                @{ path = ('acl:' + $d['Name']); kind = 'acl'; dir_path = $d.Path;
                   producer = 'S1 prepare (experiment DACL matrix)'; status = 'planned' })
            Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
                $LedgerPath, ($Ledger | ConvertTo-Json -Depth 6))
        }
        # Pre-change audit + backup (icacls /save writes the file itself).
        $probe = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @('icacls', $d.Path)
        if ($probe.ExitCode -ne 0) {
            throw ("experiment ACL audit failed for " + $d.Path + " (exit " + $probe.ExitCode + ")")
        }
        if (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($backupPath)) {
            # An orphan backup without a matching pending state refuses -
            # a prior run's recovery material is never overwritten.
            throw ("an experiment ACL backup already exists for " + $d.Path + ": " + $backupPath + "; refusing to overwrite the original recovery material")
        }
        $save = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
            'icacls', $d.Path, '/save', $backupPath)
        if ($save.ExitCode -ne 0) {
            throw ("experiment ACL backup failed for " + $d.Path + " (exit " + $save.ExitCode + "); refusing to change permissions without a recovery record")
        }
        $state['dirs'] = @(@($state['dirs']) | Where-Object { [string]$_['path'] -ne $d.Path })
        $state['dirs'] += @{ path = $d.Path; backup_rel = $backupRel; state = 'pending' }
        Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($statePath, ($state | ConvertTo-Json -Depth 5))
        Write-Log ("experiment ACL backup saved; recovery state PENDING (" + $d.Name + ")")
        # The grant: the EXACT protected matrix - /inheritance:r first
        # (a protected DACL; inherited/other ACEs are REMOVED, not merged),
        # then the explicit grants.
        $grant = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList (
            @('icacls', $d.Path, '/inheritance:r') +
            (@($d['Ace']) | ForEach-Object { @('/grant:r', $_) } | ForEach-Object { $_ }))
        if ($grant.ExitCode -ne 0) {
            # Bounded recovery: restore the pre-change ACL; a failed restore
            # keeps the backup + state=failed (operator review material) -
            # BOTH outcomes persist through the FileBoundary.
            $restore = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
                'icacls', $d.Path, '/restore', $backupPath)
            $newState = 'none'
            if ($restore.ExitCode -eq 0) {
                Write-Log ("experiment ACL change failed but the original ACL was RESTORED: " + $d.Path)
            } else {
                $newState = 'failed'
                Write-Log ("WARNING: experiment ACL restore FAILED for " + $d.Path + "; backup kept: " + $backupPath)
            }
            $state['dirs'] = @(@($state['dirs']) | ForEach-Object {
                if ([string]$_['path'] -eq $d.Path) {
                    @{ path = $_.path; backup_rel = $_.backup_rel; state = $newState }
                } else { $_ } })
            Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($statePath, ($state | ConvertTo-Json -Depth 5))
            throw ("experiment ACL grant failed for " + $d.Path + " (exit " + $grant.ExitCode + "); recovery recorded")
        }
        # Verify the landed ACL against the matrix (limited-write aware and
        # the required principals MUST be present).
        $query = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @('icacls', $d.Path)
        if ($query.ExitCode -ne 0) {
            throw ("experiment ACL re-query failed for " + $d.Path + " (exit " + $query.ExitCode + ")")
        }
        $verdict = Test-AclOutput -Output @($query.Output) -TargetPath $d.Path `
            -AllowedFull $d['Full'] -ReadOnlyIdentity $operator `
            -Forbidden @() -LimitedWrite $d['Limited']
        if (-not $verdict['Ok']) {
            throw ("experiment ACL verification failed for " + $d.Path + ": " + $verdict.Reason)
        }
        $state['dirs'] = @(@($state['dirs']) | ForEach-Object {
            if ([string]$_['path'] -eq $d.Path) {
                @{ path = $_.path; backup_rel = $_.backup_rel; state = 'applied' }
            } else { $_ } })
        Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($statePath, ($state | ConvertTo-Json -Depth 5))
        Write-Log ("experiment ACL matrix applied+verified: " + $d.Name)
        $Ledger = Update-D4IntentLedger -Ledger $Ledger -LedgerPath $LedgerPath `
            -Path ('acl:' + $d['Name']) -Status 'created' -FileBoundary $FileBoundary
    }
    return @{ Ledger = $Ledger; StatePath = $statePath }
}

function Invoke-D4RestoreExperimentAcl {
    # R69-C: drive the recorded ACL recovery to a PROVEN terminal state.
    # 'pending'/'failed' dirs are restored; with -RollbackApplied (the S1
    # abort path) 'applied' dirs are restored too - the experiment is being
    # abandoned, so every applied matrix rolls back. Every outcome persists
    # ATOMICALLY through the FileBoundary (none = restored+consumed, failed
    # = backup kept); a corrupt record or a path outside the experiment
    # root REFUSES (never log-and-continue); a missing backup persists
    # 'failed' and reports unconfirmed. Returns
    # @{ Confirmed = @(); Unconfirmed = @() }.
    param([hashtable]$Cfg, [scriptblock]$IcaclsRunner, [hashtable]$FileBoundary,
          [string]$EvidenceDir, [string]$ExperimentRoot, [switch]$RollbackApplied)
    $statePath = Join-Path $EvidenceDir 'acl-recovery.json'
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($statePath))) {
        return @{ Confirmed = @(); Unconfirmed = @() }
    }
    # R73-B: the WHOLE record is parsed and validated through the ONE shared
    # contract BEFORE any icacls runs - shape, enum, target<->backup fixed
    # pairing, uniqueness and traversal rules all apply. ONE bad item
    # refuses the whole restore with ZERO restore calls (the record bytes
    # stay on disk).
    $validated = ConvertFrom-D4AclRecoveryRecord -RawRecord (
        Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($statePath)) `
        -ExperimentRoot $ExperimentRoot
    $confirmed = @()
    $unconfirmed = @()
    # R73-A: the working set starts as the FULL validated collection - every
    # persistence write below replaces ONLY the current item's outcome and
    # ALWAYS carries the not-yet-processed items (their recovery obligation
    # can never vanish from the record between items).
    $newDirs = @($validated['Dirs'] | ForEach-Object {
        @{ path = [string]$_['path']; backup_rel = [string]$_['backup_rel']; state = [string]$_['state'] } })
    $persistedPath = Join-Path $EvidenceDir 'acl-recovery.json'
    foreach ($d in @($validated['Dirs'])) {
        # Hoist the member reads into variables - an inline [string]$d.member
        # inside a COMMAND ARGUMENT parses as the cast of $d plus a literal
        # member suffix (a real PS argument-mode trap).
        $dirPath = [string]$d.path
        $dirState = [string]$d.state
        $rel = [string]$d.backup_rel
        # The root ITSELF is a legal recovery target (its DACL is one of the
        # prepared objects) - only paths OUTSIDE the root refuse.
        $rootEq = $dirPath.Equals(([string]$ExperimentRoot), [StringComparison]::OrdinalIgnoreCase)
        if ($dirPath -and -not $rootEq -and
                -not $dirPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw ("acl recovery state path escapes the experiment root: " + $dirPath)
        }
        $wanted = 'pending', 'failed'
        if ($RollbackApplied) { $appliedToo = $true } else { $appliedToo = $false }
        if ($dirState -notin 'pending', 'failed') {
            if (-not ($appliedToo -and $dirState -eq 'applied')) {
                # R75.2: the working set ALREADY contains the full validated
                # collection - the skip branch keeps the item AS IS (its
                # state is already on the record). Appending again would
                # duplicate the target and break the next contract parse.
                continue
            }
        }
        $backupPath = Join-Path $EvidenceDir $rel
        if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($backupPath))) {
            # Missing backup while a change is recorded: the ACL state is
            # UNVERIFIABLE - persist failed and surface unconfirmed. The
            # write carries the WHOLE working set (R73-A: the not-yet-
            # processed items can never vanish between items).
            foreach ($wd in $newDirs) {
                if ([string]$wd['path'] -eq $dirPath) {
                    $wd.state = 'failed'
                }
            }
            $unconfirmed += ($dirPath + ' (acl backup missing: ' + $rel + '; state=failed persisted)')
            Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
                $persistedPath, (@{ dirs = $newDirs } | ConvertTo-Json -Depth 5))
            continue
        }
        $restore = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
            'icacls', $dirPath, '/restore', $backupPath)
        if ($restore.ExitCode -eq 0) {
            # Persist FIRST: replace THIS item in the whole working set (the
            # not-yet-processed items always stay in the record - R73-A).
            foreach ($wd in $newDirs) {
                if ([string]$wd['path'] -eq $dirPath) { $wd.state = 'none' }
            }
            $confirmed += ($dirPath + ' (restored)')
            Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
                $persistedPath, (@{ dirs = $newDirs } | ConvertTo-Json -Depth 5))
            Write-Log ("experiment ACL restored (icacls /restore): " + $dirPath)
        } else {
            foreach ($wd in $newDirs) {
                if ([string]$wd['path'] -eq $dirPath) { $wd.state = 'failed' }
            }
            $unconfirmed += ($dirPath + ' (restore FAILED exit ' + $restore.ExitCode + '; backup kept: ' + $rel + ')')
            Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
                $persistedPath, (@{ dirs = $newDirs } | ConvertTo-Json -Depth 5))
            Write-Log ("WARNING: experiment ACL restore FAILED for " + $dirPath + " (exit " + $restore.ExitCode + "); backup kept")
        }
    }
    # R71-B: the final state (whatever the last item reached) is already on
    # disk - every item persisted its own outcome immediately (below); the
    # trailing write keeps the shape identical when the record had no
    # restorable items.
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
        $persistedPath, (@{ dirs = $newDirs } | ConvertTo-Json -Depth 5))
    return @{ Confirmed = $confirmed; Unconfirmed = $unconfirmed }
}

function ConvertFrom-D4AclRecoveryRecord {
    # R73-B: the ONE parse/validate contract for the experiment ACL recovery
    # record, shared by Restore, Complete AND Prepare's prior-read. Fixed
    # shape: an object with a dirs ARRAY (a missing dirs list is corrupt -
    # never a legal empty state); every item is an object with NON-EMPTY
    # string path/backup_rel/state; state in the fixed enum; the path is one
    # of the two prepared targets; the backup is the dir's FIXED pair
    # (root->acl-backup-root.dat, run\live->acl-backup-run-live.dat); paths
    # unique and traversal-free. ANY violation throws - a bad record keeps
    # its bytes on disk and is never reinterpreted as an empty collection.
    param([string]$RawRecord, [string]$ExperimentRoot)
    $record = $null
    try {
        $record = $RawRecord | ConvertFrom-Json
    } catch {
        throw ("acl recovery record is not valid JSON: " + $_.Exception.Message)
    }
    if ($null -eq $record -or $record -isnot [pscustomobject] -or
            $null -eq $record.PSObject.Properties['dirs']) {
        throw "acl recovery record is missing the dirs list (corrupt, never an empty state)"
    }
    if ($record.dirs -isnot [array] -and $record.dirs -isnot [pscustomobject]) {
        throw ("acl recovery record dirs is not a collection: " + [string]$record.dirs)
    }
    $root = ([string]$ExperimentRoot).TrimEnd('\')
    $runDir = Join-Path $root 'run\live'
    $expectedBackup = @{
        ($root.ToLowerInvariant())  = 'acl-backup-root.dat'
        ($runDir.ToLowerInvariant()) = 'acl-backup-run-live.dat'
    }
    $seen = @{}
    $out = @()
    foreach ($item in @($record.dirs)) {
        if ($null -eq $item -or $item -isnot [pscustomobject]) {
            throw "acl recovery record entry is not an object"
        }
        $props = $item.PSObject.Properties
        foreach ($name in @('path', 'backup_rel', 'state')) {
            if ($null -eq $item.PSObject.Properties[$name]) {
                throw ("acl recovery item missing field: " + $name)
            }
        }
        $dirPath = ([string]$item.path).Trim()
        $rel = ([string]$item.backup_rel).Trim()
        $dirState = [string]$item.state
        if (-not $dirPath) { throw "acl recovery item has an EMPTY path" }
        if (-not $rel) { throw "acl recovery item has an EMPTY backup_rel" }
        if ($dirState -notin 'pending', 'applied', 'none', 'failed', 'removed') {
            throw ("acl recovery item has an unknown state '" + $dirState + "' for " + $dirPath)
        }
        if ($dirPath -like '*..*' -or $rel -like '*..*' -or $rel -like '*:*' -or $rel -like '*\*') {
            throw ("acl recovery item carries a traversal/absolute segment: " + $dirPath + " / " + $rel)
        }
        $matched = $false
        foreach ($k in @($expectedBackup.Keys)) {
            if ($dirPath.Equals($k, [StringComparison]::OrdinalIgnoreCase)) {
                $matched = $true
                if ($rel -ne $expectedBackup[$k]) {
                    throw ("acl recovery backup PAIRING mismatch for " + $dirPath +
                           ": expected '" + $expectedBackup[$k] + "', got '" + $rel + "'")
                }
            }
        }
        if (-not $matched) {
            throw ("acl recovery item targets a NON-reviewed object: " + $dirPath +
                   "; only the experiment root and run\live are prepared objects")
        }
        $dupKey = $dirPath.ToLowerInvariant()
        if ($seen.ContainsKey($dupKey)) {
            throw ("acl recovery record DUPLICATE target: " + $dirPath)
        }
        $seen[$dupKey] = $true
        $out += @{ path = $dirPath; backup_rel = $rel; state = $dirState }
    }
    return @{ Dirs = $out }
}

function Invoke-D4CompleteExperimentAcl {
    # R73-B: the finalize uses the SAME shared record contract as Restore -
    # shape, enum, target<->backup fixed pairing, uniqueness; a corrupt
    # record REFUSES here too (never read as an empty collection and
    # rewritten). The finalize NEVER claims a removal without proof: every
    # applied entry is confirmed against the REAL filesystem (FileBoundary
    # Exists) - only a dir that is ACTUALLY GONE records removed; a dir
    # still present KEEPS its applied state and is REPORTED; pending/failed
    # entries are never hidden. Returns @{ Removed; Kept; Unresolved }.
    param([hashtable]$FileBoundary, [string]$EvidenceDir, [string]$ExperimentRoot)
    $statePath = Join-Path $EvidenceDir 'acl-recovery.json'
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($statePath))) {
        return @{ Removed = @(); Kept = @(); Unresolved = @() }
    }
    $validated = ConvertFrom-D4AclRecoveryRecord -RawRecord (
        Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($statePath)) `
        -ExperimentRoot $ExperimentRoot
    $removed = @()
    $kept = @()
    $unresolved = @()
    # R73-A: the working set starts as the FULL validated collection; the
    # finalize replaces ONLY each item's fact and persists the whole set.
    $newDirs = @($validated['Dirs'] | ForEach-Object {
        @{ path = [string]$_['path']; backup_rel = [string]$_['backup_rel']; state = [string]$_['state'] } })
    foreach ($d in @($validated['Dirs'])) {
        $dirPath = [string]$d.path
        $dirState = [string]$d.state
        foreach ($wd in $newDirs) {
            if ([string]$wd['path'] -ne $dirPath) { continue }
            if ($dirState -eq 'applied') {
                if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($dirPath))) {
                    # The dir is REALLY gone - only then is the protection
                    # fact recorded as removed.
                    $wd.state = 'removed'
                    $removed += $dirPath
                } else {
                    # The dir SURVIVED (evidence retention, removal failure):
                    # the applied protection is still live - report it.
                    $wd.state = 'applied'
                    $kept += ($dirPath + ' (dir still present; applied protection remains live)')
                }
            } elseif ($dirState -in 'pending', 'failed') {
                # Never hidden: an unresolved recovery stays visible and
                # blocks the pass via the Unresolved list.
                $unresolved += ($dirPath + ' (state=' + $dirState + '; recovery still owed)')
            } else {
                $wd.state = $dirState
            }
        }
    }
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @(
        (Join-Path $EvidenceDir 'acl-recovery.json'), (@{ dirs = $newDirs } | ConvertTo-Json -Depth 5))
    Write-Log ("experiment ACL state finalized (removed=" + @($removed).Count +
               " kept=" + @($kept).Count + " unresolved=" + @($unresolved).Count + ")")
    return @{ Removed = $removed; Kept = $kept; Unresolved = $unresolved }
}function New-D4RealRulesRunner {
    # R67-A/B: the REAL limit-rule operator over fwpuclnt.dll. The specs
    # travel INSIDE the closure captured from the reviewed config - free
    # parameters can never drive the gate. Verbs:
    #   @('limits','verify',<id>)  -> structured FACTS of the live filter
    #                                 (layer/action/conds/app-id/scope);
    #                                 ABSENT exits 1; query failure exits 2.
    #   @('limits','create',<id>)  -> add per the spec (FwpmFilterAdd0);
    #                                 exists+matching = idempotent 0;
    #                                 exists+foreign = exit 3 (no takeover);
    #                                 failure = exit 2.
    #   @('limits','remove',<id>)  -> facts must MATCH first (identity),
    #                                 then FwpmFilterDeleteByKey0; absent =
    #                                 exit 0 (idempotent); foreign = exit 3.
    #   @('limits','list',<ids>)   -> PRESENT/ABSENT per requested id.
    # The x64 FWPM_FILTER0 offsets follow the reviewed fwpmu.h survey
    # (200-byte struct; action.type @0x80; 40-byte conditions with the
    # value union @+8 of each condition; the app-id blob is the
    # FwpmGetAppIdFromFileName0 normalization, byte-compared).
    param([object[]]$RuleSpecs, [hashtable]$Contract, [hashtable]$NativeOps)
    $specs = @()
    foreach ($raw in @($RuleSpecs)) { $specs += @(ConvertTo-D4RuleSpec -Raw $raw) }
    if (-not ([System.Management.Automation.PSTypeName]'UmD4RuleOps').Type) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Collections.Generic;

// R69-A: the native layer is split into a PURE codec (struct encode/decode,
// fully offline-testable against controlled memory) and the thin fwpuclnt
// P/Invoke surface. The codec owns ALL pointer math; scalar values
// (UINT32/UINT16) and pointer values (BYTE_BLOB) never share a
// dereference path.
public static class UmD4RuleCodec {
    public const uint ACT_PERMIT = 0x1002;
    public const uint ACT_BLOCK = 0x1001;
    public const uint T_UINT16 = 2;
    public const uint T_UINT32 = 3;
    public const uint T_UINT64 = 4;
    public const uint T_BYTE_BLOB = 12;
    public const uint MATCH_EQUAL = 0;
    public static readonly Guid FAppId = new Guid("d78e1e87-8644-4ea5-9437-d809ecefc971");
    public static readonly Guid FAddr = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");
    public static readonly Guid FPort = new Guid("c35a604d-d22b-4e1a-91b4-68f674ee674b");
    public static readonly Guid SubUniversal = new Guid("eebecc03-ced4-4380-819a-2734397b2b74");
    public const int FILTER_SIZE = 200;
    public const int COND_SIZE = 40;
    public static readonly Guid LayerV4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
    public static readonly Guid LayerV6 = new Guid("4a72393b-319f-44bc-84c3-ba54dcb3b6b4");

    // The codec's OWNED native buffer: every sub-allocation is tracked and
    // freed exactly once by Dispose (allocation-failure paths included).
    public class NativeFilter : IDisposable {
        public IntPtr Buffer;                  // the 200-byte FWPM_FILTER0
        private readonly List<IntPtr> blocks = new List<IntPtr>();
        // R71-A: the allocator is INJECTABLE (offline fixtures inject a
        // deterministic failure on the Nth allocation); LiveAllocations
        // gives the alloc/free balance as test evidence - CLR exceptions do
        // NOT imply HGlobal reclamation, so the balance is proven here.
        public static Func<int, IntPtr> Allocator = (size) => Marshal.AllocHGlobal(size);
        public static int LiveAllocations = 0;
        private IntPtr Track(int size) {
            IntPtr p = Allocator(size);
            if (p == IntPtr.Zero) { throw new OutOfMemoryException("allocator returned a null block"); }
            LiveAllocations++;
            blocks.Add(p);
            return p;
        }
        private void ReleaseAll() {
            foreach (IntPtr p in blocks) { Marshal.FreeHGlobal(p); LiveAllocations--; }
            blocks.Clear();
            Buffer = IntPtr.Zero;
        }
        public NativeFilter(Guid key, string name, Guid layerGuid, uint action,
            byte[] appIdData, bool withScope, uint addr, ushort port, Guid subLayer) {
            try {
                Buffer = Track(FILTER_SIZE);
                for (int i = 0; i < FILTER_SIZE; i++) { Marshal.WriteByte(Buffer, i, 0); }
                // R71-A: EVERY allocation is registered IMMEDIATELY after it
                // succeeds (no batching window between name and desc). The
                // string blocks carry the UTF-16 NULL terminator.
                IntPtr namePtr = Track((name.Length + 1) * 2);
                byte[] nameBytes = System.Text.Encoding.Unicode.GetBytes(name);
                Marshal.Copy(nameBytes, 0, namePtr, nameBytes.Length);
                Marshal.WriteInt16(namePtr, nameBytes.Length, 0);
                Marshal.WriteIntPtr(Buffer, 0x10, namePtr);
                IntPtr descPtr = Track(("UM share D4 experiment isolation (app-scoped)".Length + 1) * 2);
                byte[] descBytes = System.Text.Encoding.Unicode.GetBytes(
                    "UM share D4 experiment isolation (app-scoped)");
                Marshal.Copy(descBytes, 0, descPtr, descBytes.Length);
                Marshal.WriteInt16(descPtr, descBytes.Length, 0);
                Marshal.WriteIntPtr(Buffer, 0x18, descPtr);
                byte[] keyBytes = key.ToByteArray();                  // 0x00 filterKey
                Marshal.Copy(keyBytes, 0, Buffer, 16);
                Marshal.WriteIntPtr(Buffer, 0x28, IntPtr.Zero);       // providerKey
                Marshal.WriteInt32(Buffer, 0x30, 0);                  // providerData.size
                Marshal.WriteIntPtr(Buffer, 0x38, IntPtr.Zero);       // providerData.data
                byte[] layerBytes = layerGuid.ToByteArray();
                Marshal.Copy(layerBytes, 0, new IntPtr(Buffer.ToInt64() + 0x40), 16);
                byte[] subBytes = subLayer.ToByteArray();
                Marshal.Copy(subBytes, 0, new IntPtr(Buffer.ToInt64() + 0x50), 16);
                Marshal.WriteInt32(Buffer, 0x60, (int)T_UINT64);      // weight type
                // R67-A arbitration (W2 run20 root cause): with EXPLICIT equal
                // weights the engine's tie-break prefers BLOCK over PERMIT, so
                // the scoped permit was unreachable and the app was fully
                // contained (observed: WSAEACCES 10013 on the permitted
                // loopback connect). The permit carries base+1 so the scoped
                // destination wins its carve-out; every other destination
                // still matches only the block rule.
                IntPtr weightPtr = Track(8);
                Marshal.WriteInt64(weightPtr, unchecked((long)
                    (0x0000F00000000000UL + (withScope ? 1UL : 0UL))));
                Marshal.WriteIntPtr(Buffer, 0x68, weightPtr);
                int condN = (action == ACT_PERMIT) ? 3 : 1;
                Marshal.WriteInt32(Buffer, 0x70, condN);
                IntPtr condArray = Track(condN * COND_SIZE);
                Marshal.WriteIntPtr(Buffer, 0x78, condArray);
                Marshal.WriteInt32(Buffer, 0x80, (int)action);        // action.type @0x80
                for (int i = 0; i < condN; i++) {
                    IntPtr c = new IntPtr(condArray.ToInt64() + (long)i * COND_SIZE);
                    Guid field = (i == 0) ? FAppId : ((i == 1) ? FAddr : FPort);
                    Marshal.Copy(field.ToByteArray(), 0, c, 16);
                    Marshal.WriteInt32(c, 0x10, (int)MATCH_EQUAL);
                    if (i == 0) {
                        // BYTE_BLOB: the union field holds a POINTER to the
                        // FWP_BYTE_BLOB wrapper {size@0, data@8}.
                        Marshal.WriteInt32(c, 0x18, (int)T_BYTE_BLOB);
                        IntPtr blobWrapper = Track(16);
                        IntPtr dataPtr = Track(appIdData.Length);
                        Marshal.WriteInt32(blobWrapper, 0, appIdData.Length);
                        Marshal.WriteIntPtr(blobWrapper, 8, dataPtr);
                        Marshal.Copy(appIdData, 0, dataPtr, appIdData.Length);
                        Marshal.WriteIntPtr(c, 0x20, blobWrapper);
                    } else if (i == 1) {
                        // UINT32 is stored INLINE in the union (value at 0x20) -
                        // never a pointer (R69-A-3: no shared dereference).
                        Marshal.WriteInt32(c, 0x18, (int)T_UINT32);
                        Marshal.WriteInt32(c, 0x20, unchecked((int)addr));
                    } else {
                        Marshal.WriteInt32(c, 0x18, (int)T_UINT16);
                        Marshal.WriteInt16(c, 0x20, (short)port);
                    }
                }
            } catch {
                // R71-A: a failed constructor frees every REGISTERED block
                // before rethrowing - the caller never reaches its finally
                // with a half-built object, and HGlobals do not get reclaimed
                // by the CLR.
                ReleaseAll();
                throw;
            }
        }
        public void Dispose() {
            foreach (IntPtr p in blocks) { Marshal.FreeHGlobal(p); LiveAllocations--; }
            blocks.Clear();
            Buffer = IntPtr.Zero;
        }
    }

    // The live filter's STRUCTURED FACTS decoded from a 200-byte image.
    // SCALAR condition values are read INLINE at the union (@0x20 of the
    // condition); pointer values (BYTE_BLOB) are dereferenced as pointers -
    // the two families NEVER share a dereference algorithm (R69-A-3).
    public static string FactsFromBytes(byte[] filter, byte[] expectedAppId) {
        byte[] layerBytes = new byte[16];
        Array.Copy(filter, 0x40, layerBytes, 0, 16);
        string layer = new Guid(layerBytes).ToString();
        uint action = (uint)BitConverter.ToInt32(filter, 0x80);
        uint condCount = (uint)BitConverter.ToInt32(filter, 0x70);
        IntPtr conds = new IntPtr(BitConverter.ToInt64(filter, 0x78));
        string appid = "NONE";
        string addr = "NA";
        string port = "NA";
        for (uint i = 0; i < condCount; i++) {
            IntPtr c = new IntPtr(conds.ToInt64() + (long)i * COND_SIZE);
            byte[] fieldBytes = new byte[16];
            Marshal.Copy(c, fieldBytes, 0, 16);
            Guid field = new Guid(fieldBytes);
            uint matchType = (uint)Marshal.ReadInt32(c, 0x10);
            uint valueType = (uint)Marshal.ReadInt32(c, 0x18);
            if (field == FAppId && valueType == T_BYTE_BLOB && matchType == MATCH_EQUAL) {
                IntPtr union = Marshal.ReadIntPtr(c, 0x20);      // pointer value
                IntPtr blobData = Marshal.ReadIntPtr(union, 8);
                uint blobSize = (uint)Marshal.ReadInt32(union, 0);
                appid = "MISMATCH";
                if (blobSize == (uint)expectedAppId.Length) {
                    bool equal = true;
                    for (uint b = 0; b < blobSize; b++) {
                        if (Marshal.ReadByte(blobData, (int)b) != expectedAppId[b]) { equal = false; break; }
                    }
                    appid = equal ? "MATCH" : "MISMATCH";
                }
            } else if (field == FAddr && valueType == T_UINT32 && matchType == MATCH_EQUAL) {
                addr = Marshal.ReadInt32(c, 0x20).ToString();    // INLINE scalar
            } else if (field == FPort && valueType == T_UINT16 && matchType == MATCH_EQUAL) {
                port = Marshal.ReadInt16(c, 0x20).ToString();    // INLINE scalar
            }
        }
        return "layer=" + layer + " action=0x" + action.ToString("x4") +
               " conds=" + condCount + " appid=" + appid +
               " addr=" + addr + " port=" + port;
    }

    // Live-pointer overload: copy the BFE-owned struct into managed memory
    // and decode through the SAME pure path (no second decode algorithm).
    public static string Facts(IntPtr filter, byte[] expectedAppId) {
        byte[] filterBytes = new byte[FILTER_SIZE];
        Marshal.Copy(filter, filterBytes, 0, FILTER_SIZE);
        return FactsFromBytes(filterBytes, expectedAppId);
    }
}

public static class UmD4RuleOps {
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmEngineOpen0(IntPtr serverName, uint authnService,
        IntPtr authIdentity, IntPtr session, out IntPtr engineHandle);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterAdd0(IntPtr engineHandle, IntPtr filter,
        IntPtr sd, out ulong id);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterDeleteByKey0(IntPtr engineHandle, ref Guid filterKey);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterGetByKey0(IntPtr engineHandle, ref Guid filterKey,
        ref IntPtr filter);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmGetAppIdFromFileName0(string fileName, out IntPtr appIdBlob);
    [DllImport("fwpuclnt.dll")]
    public static extern uint FwpmFreeMemory0(ref IntPtr p);
    [DllImport("fwpuclnt.dll")]
    public static extern uint FwpmEngineClose0(IntPtr engineHandle);

    // R69-A: the encode/build and the API call are separate; the out id is
    // the LAST parameter and every input is bound explicitly. The native
    // buffer (built by the codec) is disposed by the CALLER after Add
    // returns (BFE copies the filter and its data at Add time).
    public static uint AddIsolationFilter(IntPtr engine, UmD4RuleCodec.NativeFilter native,
        out ulong id) {
        id = 0;
        return FwpmFilterAdd0(engine, native.Buffer, IntPtr.Zero, out id);
    }
}
"@
    }
    $specById = @{}
    foreach ($s in $specs) { $specById[[string]$s['id']] = $s }
    # The rc constants are captured as LOCALS (never '$script:' inside the
    # returned closure - a GetNewClosure block resolves '$script:' against
    # its own module scope, not the library's). The layer/sublayer maps are
    # captured the same way - an in-closure '$script:' read resolves to NULL
    # and the real runner's first create would crash on the layer lookup.
    $rcFilterNotFound = $script:D4FwpRcFilterNotFound
    $rcAlreadyExists = $script:D4FwpRcAlreadyExists
    $layerMap = $script:D4WfpLayerGuids
    $subLayerGuid = [Guid]$script:D4WfpSubLayer
    # R78-D: the NATIVE surface is injectable for the offline decision
    # tests - ONLY the OS calls are replaceable (the reviewed spec/id gate,
    # the parameter conversion, the facts decode/compare and the ownership
    # classification run unmodified). A supplied surface must be COMPLETE -
    # a half-injected fixture can never pass silently.
    if ($NativeOps) {
        foreach ($k in @('EngineOpen', 'GetAppId', 'GetByKey', 'Add', 'Delete', 'Free', 'Close')) {
            if (-not $NativeOps.ContainsKey($k)) {
                throw ('the injected native surface is incomplete (missing: ' + $k + ')')
            }
        }
    }
    return {
        param([string[]]$ArgumentList)
        $ErrorActionPreference = 'Stop'
        if (@($ArgumentList).Count -lt 2 -or $ArgumentList[0] -ne 'limits') {
            return (New-TestRunResult -ExitCode 2 -Output @('unsupported limits runner invocation'))
        }
        $verb = $ArgumentList[1]
        $ids = @($ArgumentList | Select-Object -Skip 2)
        foreach ($id in $ids) {
            if (-not $specById.ContainsKey($id)) {
                return (New-TestRunResult -ExitCode 2 -Output @('unsupported limit id (not in the reviewed config): ' + $id))
            }
        }
        $engine = [IntPtr]::Zero
        if ($NativeOps) {
            $rc = & $NativeOps['EngineOpen'] ([ref]$engine)
        } else {
            $rc = [UmD4RuleOps]::FwpmEngineOpen0([IntPtr]::Zero, 10, [IntPtr]::Zero,
                [IntPtr]::Zero, [ref]$engine)
        }
        if ($rc -ne 0) {
            return (New-TestRunResult -ExitCode 2 -Output @(('FwpmEngineOpen0 failed: 0x{0:X}' -f $rc)))
        }
        $out = @()
        $exitCode = 0
        try {
            # The expected app-id bytes come from BFE itself (the same
            # normalization the OS applies), read ONCE into managed memory
            # from the FIRST id's spec path - every spec in the reviewed
            # config pins the SAME image. The 'list' verb NEVER consumes the
            # app-id (it compares filter KEYS only), and its queries run in
            # the cleanup's final verdict AFTER the experiment image files
            # were removed - FwpmGetAppIdFromFileName0 fails on a nonexistent
            # file (0x2, W2 run20) - so the normalization is lazily gated on
            # the verbs that actually consume the bytes.
            $appIdBytes = $null
            $blob = [IntPtr]::Zero
            if ($verb -ne 'list') {
            $appPath = [string]($specById[$ids[0]]['app_path'])
            if ($NativeOps) {
                $rc = & $NativeOps['GetAppId'] $appPath ([ref]$blob)
            } else {
                $rc = [UmD4RuleOps]::FwpmGetAppIdFromFileName0($appPath, [ref]$blob)
            }
            if ($rc -ne 0) {
                return (New-TestRunResult -ExitCode 2 -Output @(('app-id normalization failed: 0x{0:X}' -f $rc)))
            }
            $appIdBytes = [byte[]]::CreateInstance([byte], [int][UInt32][Runtime.InteropServices.Marshal]::ReadInt32($blob, 0))
            $blobData = [Runtime.InteropServices.Marshal]::ReadIntPtr($blob, 8)
            for ($bi = 0; $bi -lt $appIdBytes.Length; $bi++) {
                $appIdBytes[$bi] = [byte][Runtime.InteropServices.Marshal]::ReadByte($blobData, $bi)
            }
            }
            foreach ($id in $ids) {
                    $spec = $specById[$id]
                    $key = [Guid]::Parse($id)
                    $filter = [IntPtr]::Zero
                    if ($NativeOps) {
                        $grc = & $NativeOps['GetByKey'] $engine ([ref]$key) ([ref]$filter)
                    } else {
                        $grc = [UmD4RuleOps]::FwpmFilterGetByKey0($engine, [ref]$key, [ref]$filter)
                    }
                    $present = ($grc -eq 0)
                    if ($verb -eq 'list') {
                        if ($filter -ne [IntPtr]::Zero) {
                            $tmp = $filter
                            if ($NativeOps) { & $NativeOps['Free'] ([ref]$tmp) } else { [void][UmD4RuleOps]::FwpmFreeMemory0([ref]$tmp) }
                            $filter = [IntPtr]::Zero
                        }
                        if ($grc -eq 0) { $out += ('PRESENT ' + $id) }
                        elseif ($grc -eq $rcFilterNotFound) { $out += ('ABSENT ' + $id) }
                        else { $exitCode = 2; $out += ('QUERY-FAILED 0x{0:X} {1}' -f $grc, $id) }
                        continue
                    }
                    if ($grc -eq $rcFilterNotFound) {
                        $filter = [IntPtr]::Zero
                    } elseif ($grc -ne 0) {
                        return (New-TestRunResult -ExitCode 2 -Output @(('QUERY-FAILED 0x{0:X} {1}' -f $grc, $id)))
                    }
                    $present = ($filter -ne [IntPtr]::Zero)
                    $facts = ''
                    if ($present) {
                        try {
                            $facts = [UmD4RuleCodec]::Facts($filter, $appIdBytes)
                        } finally {
                            $tmp = $filter
                            if ($NativeOps) { & $NativeOps['Free'] ([ref]$tmp) } else { [void][UmD4RuleOps]::FwpmFreeMemory0([ref]$tmp) }
                        }
                    }
                    if ($verb -eq 'verify') {
                        if ($present) { $out += ('PRESENT ' + $id + ' ' + $facts) }
                        else { $exitCode = 1; $out += ('ABSENT ' + $id) }
                    } elseif ($verb -eq 'create') {
                        $spec = $specById[$id]
                        $layer = [Guid]$layerMap[[string]$spec['layer']]
                        # The ACT_* constants live on the CODEC class - the
                        # P/Invoke class has no such members and a missing
                        # static resolves to $null (action written as 0,
                        # condN collapsed to 1 - the fake-native decision
                        # fixtures surfaced this).
                        $action = [UmD4RuleCodec]::ACT_PERMIT
                        if ([string]$spec['action'] -eq 'block') { $action = [UmD4RuleCodec]::ACT_BLOCK }
                        $withScope = ([string]$spec['action'] -eq 'permit')
                        if ($withScope) {
                            # R69-A: ONLY a permit rule carries the scope -
                            # a block spec has no addr/port fields and this
                            # path never reads them (the octets convert
                            # numerically; the array is never stringified).
                            $octets = @($spec['remote_addr'] -split '\.')
                            if ($octets.Count -ne 4) {
                                return (New-TestRunResult -ExitCode 2 -Output @('permit scope addr malformed: ' + [string]$spec['remote_addr']))
                            }
                            $addr = [uint32]([uint32]$octets[0] * 16777216 + [uint32]$octets[1] * 65536 +
                                             [uint32]$octets[2] * 256 + [uint32]$octets[3])
                            $port = [uint16][int]$spec['remote_port']
                        } else {
                            $addr = [uint32]0
                            $port = [uint16]0
                        }
                        if ($present) {
                            $verdict = Test-D4RuleFactsMatch -Spec $spec -FactsText $facts -Contract $Contract
                            if ($verdict['Ok']) { $out += ('ALREADY-PRESENT ' + $id + ' (facts match)') }
                            else { $exitCode = 3; $out += ('FOREIGN ' + $id + ': ' + $verdict['Reason']) }
                        } else {
                            $name = 'um-share-exp-' + [string]$spec['action'] + '-' + $id.Substring(0, 8)
                            $layer = [Guid]$layerMap[[string]$spec['layer']]
                            $subLayer = $subLayerGuid
                            $native = New-Object UmD4RuleCodec+NativeFilter(
                                $key, $name, $layer, $action, $appIdBytes,
                                $withScope, $addr, $port, $subLayer)
                            try {
                                $newId = [uint64]0
                                if ($NativeOps) {
                                    $addRc = & $NativeOps['Add'] $engine $native ([ref]$newId)
                                } else {
                                    $addRc = [UmD4RuleOps]::AddIsolationFilter($engine, $native, [ref]$newId)
                                }
                            } finally {
                                $native.Dispose()
                            }
                            if ($addRc -eq 0) {
                                $out += ('CREATED ' + $id)
                            } elseif ($addRc -eq $rcAlreadyExists) {
                                # R71-A: already-exists (a race: the query saw
                                # the key absent, another creator inserted
                                # before our Add) is a DISTINCT result - never
                                # reported as CREATED, never granted ownership;
                                # the ledger gate in Invoke-D4PrepareLimits
                                # applies the same ownership test.
                                $out += ('ALREADY-PRESENT ' + $id + ' (race: another creator inserted between query and add)')
                            } else {
                                $exitCode = 2
                                $out += ('CREATE-FAILED 0x{0:X} {1}' -f $addRc, $id)
                            }
                        }
                    } elseif ($verb -eq 'remove') {
                        if (-not $present) { $out += ('ABSENT ' + $id + ' (idempotent)'); continue }
                        $verdict = Test-D4RuleFactsMatch -Spec $specById[$id] -FactsText $facts -Contract $Contract
                        if (-not $verdict['Ok']) {
                            $exitCode = 3
                            $out += ('FOREIGN ' + $id + ': ' + $verdict['Reason'])
                            continue
                        }
                        if ($NativeOps) {
                            $dRc = & $NativeOps['Delete'] $engine ([ref]$key)
                        } else {
                            $dRc = [UmD4RuleOps]::FwpmFilterDeleteByKey0($engine, [ref]$key)
                        }
                        if ($dRc -ne 0) {
                            $exitCode = 2
                            $out += ('DELETE-FAILED 0x{0:X} {1}' -f $dRc, $id)
                        } else {
                            $out += ('REMOVED ' + $id)
                        }
                    }
                }
            } finally {
                $tmpBlob = $blob
                if ($NativeOps) {
                    & $NativeOps['Free'] ([ref]$tmpBlob)
                    & $NativeOps['Close'] $engine
                } else {
                    [void][UmD4RuleOps]::FwpmFreeMemory0([ref]$tmpBlob)
                    [void][UmD4RuleOps]::FwpmEngineClose0($engine)
                }
            }
            return (New-TestRunResult -ExitCode $exitCode -Output $out)
    }.GetNewClosure()
}
function New-D4RealProbeRunner {
    # R62-E: the REAL probe runner for the production entry - a FIXED
    # structure invocation of the experiment image running the reviewed
    # d4_stage_probe.py (self-rooted; the working directory is pinned to the
    # experiment root). NO scriptblock payloads and NO free script paths are
    # accepted: the subcommand and every argument are constructed here from
    # the reviewed contract, never from caller input.
    param([hashtable]$Contract, [hashtable]$Config)
    $root = [string]$Contract['ExperimentRoot']
    $image = Join-Path $root ([string]$Contract['ShareImage'])
    $probeScript = Join-Path $root 'd4_stage_probe.py'
    $pythonHome = [string]$Config['python_home']
    return {
        param([string[]]$ArgumentList)
        # Continue, not Stop: PS 5.1 turns native stderr merged via 2>&1 into
        # a terminating NativeCommandError under Stop - the probe's expected
        # failure output (negative gate, refusal text) must stay a captured
        # record; $LASTEXITCODE remains the verdict channel.
        $ErrorActionPreference = 'Continue'
        $home = $env:PYTHONHOME
        try {
            $env:PYTHONHOME = $PythonHome
            $env:PYTHONDONTWRITEBYTECODE = '1'
            $out = & $Image $ProbeScript @($ArgumentList | Select-Object -Skip 1) 2>&1
            return (New-TestRunResult -ExitCode $LASTEXITCODE -Output @($out))
        } finally {
            $env:PYTHONHOME = $home
        }
    }.GetNewClosure()
}

function Invoke-D4FaultScenarioAclChange {
    # E4 boundary 1 (ACL change, §60.5): an install whose icacls /save
    # SUCCEEDS but whose ACL set FAILS must refuse (the control secret is
    # never written). The E4 boundary RE-ENTERS over the experiment's OWN
    # S1 install (the broker dir pre-exists): R51.3 then materializes the
    # /save backup + pending record BEFORE the change, and the refused
    # install's catch records the outcome in a LEGAL terminal state
    # (R53.2) - 'none' when the rollback succeeded (backup consumed) or
    # 'failed' when the rollback was blocked (backup KEPT as evidence). A
    # reentry install resolves either outcome and completes. A FRESH root
    # has no pre-existing ACL to protect, so no recovery record exists -
    # the scenario refuses with a precondition error instead of claiming
    # recovery evidence. Returns the verdicts - never a fake success.
    param([string]$RunRoot, [string]$SourceRoot, [hashtable]$Cfg,
          [scriptblock]$FailingIcacls, [scriptblock]$GoodIcacls,
          [scriptblock]$SchtasksRunner)
    $verdicts = @()
    $statePath = Join-Path (Join-Path $RunRoot 'broker') 'install-state.json'
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw ("acl-change scenario requires the experiment's own install (reentry over S1): " + $statePath + " does not exist")
    }
    try {
        Install-Broker -Cfg $Cfg -SchtasksRunner $SchtasksRunner -IcaclsRunner $FailingIcacls
        throw ("acl-change scenario FAILED: the install with a failing ACL change did not refuse: " + $RunRoot)
    } catch {
        $msg = $_.Exception.Message
        if ($msg -like "*acl-change scenario FAILED*" -or $msg -like "*requires the experiment's own install*") { throw $msg }
        $verdicts += 'refused'
    }
    # The refused install must leave a LEGAL recovery record: the ACL
    # recovery outcome of the pre-existing directory is explicitly recorded
    # in a terminal state ('none' = rolled back, 'failed' = backup kept;
    # the transient 'pending' can never be the resting state). A
    # missing/illegal record is the defect - never a pass.
    $rec = $null
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $record = Get-AclRecoveryRecord -Manifest ([IO.File]::ReadAllText($statePath) | ConvertFrom-Json)
        if ($null -ne $record -and $record.State -in 'none', 'failed') { $rec = $record }
    }
    if ($null -eq $rec) {
        throw ("acl-change scenario FAILED: no legal recovery record after the refused install: " + $RunRoot)
    }
    $verdicts += 'recovery-recorded'
    # Reentry: the GOOD runner resolves any kept material (icacls /restore)
    # and completes. A kept backup must be consumed - never survive.
    Install-Broker -Cfg $Cfg -SchtasksRunner $SchtasksRunner -IcaclsRunner $GoodIcacls
    $backupPath = Get-ManagedPath -TargetRoot $RunRoot -RelPath 'broker-pre-acl.dat'
    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
        throw ("acl-change scenario FAILED: a kept backup file survived the reentry: " + $backupPath)
    }
    $verdicts += 'reentry-recovery-consumed'
    return $verdicts
}

function Invoke-D4FaultScenarioBackupCleanup {
    # E4 boundary 2 (backup cleanup): a crash GC window leaves a backup file
    # behind while the manifest already records state=none (consumed). The
    # reentry install removes it as GC and proceeds - it NEVER auto-restores
    # closed recovery material, and it never overwrites the record.
    param([string]$RunRoot, [string]$SourceRoot, [hashtable]$Cfg,
          [scriptblock]$GoodIcacls, [scriptblock]$SchtasksRunner)
    Install-Broker -Cfg $Cfg -SchtasksRunner $SchtasksRunner -IcaclsRunner $GoodIcacls
    $statePath = Join-Path (Join-Path $RunRoot 'broker') 'install-state.json'
    $backupPath = Get-ManagedPath -TargetRoot $RunRoot -RelPath 'broker-pre-acl.dat'
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw ("backup-cleanup scenario FAILED: the install manifest is missing: " + $statePath)
    }
    # Simulate the crash window: the terminal state is already 'none', but
    # the backup file survived between record and delete.
    Set-AclRecoveryState -StatePath $statePath -State 'none' -BackupRel 'broker-pre-acl.dat'
    [IO.File]::WriteAllText($backupPath, 'SIMULATED-CRASH-GC-BACKUP',
        (New-Object System.Text.UTF8Encoding($false)))
    $verdicts = @()
    # Reentry: the state=none surviving backup is crash GC - removed, never restored.
    Install-Broker -Cfg $Cfg -SchtasksRunner $SchtasksRunner -IcaclsRunner $GoodIcacls
    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
        throw ("backup-cleanup scenario FAILED: the state=none surviving backup was NOT collected as GC: " + $backupPath)
    }
    $verdicts += 'recovery-recorded'
    $verdicts += 'gc-collected'
    return $verdicts
}

function Invoke-D4FaultScenarioTaskRegister {
    # E4 boundary 3 (task register): a /Create failure at registration must
    # abort the install with the manifest retained (every created entry
    # recorded), and the reentry install must complete WITHOUT rebuilding
    # owned artifacts (idempotent ownership; the wrapper/definition stay
    # this install's own).
    param([string]$RunRoot, [string]$SourceRoot, [hashtable]$Cfg,
          [scriptblock]$GoodIcacls, [scriptblock]$FailingSchtasks,
          [scriptblock]$GoodSchtasks)
    try {
        Install-Broker -Cfg $Cfg -SchtasksRunner $FailingSchtasks -IcaclsRunner $GoodIcacls
        throw ("task-register scenario FAILED: the install with a failing /Create did not refuse: " + $RunRoot)
    } catch {
        $msg = $_.Exception.Message
        if ($msg -like "*task-register scenario FAILED*") { throw $msg }
    }
    $statePath = Join-Path (Join-Path $RunRoot 'broker') 'install-state.json'
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw ("task-register scenario FAILED: the manifest was NOT retained after the failed registration")
    }
    # Reentry: the GOOD runner completes; the owned artifacts stay owned
    # (identical content is reused, never rewritten as a foreign object).
    Install-Broker -Cfg $Cfg -SchtasksRunner $GoodSchtasks -IcaclsRunner $GoodIcacls
    return @('refused', 'recovery-recorded', 'reentry-completed')
}

function Test-D4TaskDefinitionShape {
    # §97.2: the SHARED reviewed full-definition shape verifier (R62-C grade):
    # exactly ONE Exec action, exactly ONE Principal, exact wrapper/command
    # path, empty arguments, NO WorkingDirectory, exact principal SID, the
    # expected LogonType AND its implied RunLevel (Password ->
    # LeastPrivilege, S4U -> HighestAvailable), trigger shape and
    # AllowStartOnDemand. The logon context comes FROM THE TASK SPEC (the
    # single source of truth) and the -LogonType parameter, when provided,
    # must MATCH it - a spec without any logon context refuses (fail-closed,
    # §97.2: no implicit bypass). EVERY claim/own/exempt/cleanup/start
    # decision consumes THIS one verifier.
    # §210.3 W1: the exported <UserId> is resolved to a canonical SID (SID
    # literal or account name) via the injectable real identity-resolution
    # edge (Resolve-D4TaskPrincipalSid, defined in the broker install lib
    # which this lib dot-sources) before it is compared to the expected
    # IdentitySid - a name string is never compared to a SID string, and a
    # group/foreign principal that cannot resolve is unconfirmed/mismatch,
    # never 'owned'.
    param([string]$DefinitionXml, [hashtable]$Task, [string]$Trigger,
          [string]$LogonType = '', [scriptblock]$IdentityResolver = $null)
    if (-not $IdentityResolver) {
        $IdentityResolver = { param([string]$t) Resolve-D4TaskPrincipalSid -PrincipalText $t }.GetNewClosure()
    }
    $expectedLogon = [string]$task['LogonType']
    if (-not $expectedLogon) { $expectedLogon = $LogonType }
    if (-not $expectedLogon) {
        return @{ State = 'mismatch'; Reason = 'logonType missing from the task spec (the logon context is mandatory)' }
    }
    if ($LogonType -and $LogonType -ne $expectedLogon) {
        return @{ State = 'mismatch'; Reason = ('logonType param/spec disagree: ' + $LogonType + ' vs ' + $expectedLogon) }
    }
    try {
        $xml = [xml]$DefinitionXml
        $actionElements = @()
        foreach ($child in $xml.Task.Actions.ChildNodes) {
            if ($child.NodeType -ne 'Element') { continue }
            $actionElements += $child.LocalName
        }
        if ($actionElements.Count -ne 1 -or $actionElements[0] -ne 'Exec') {
            return @{ State = 'mismatch'; Reason = ('actions=' + ($actionElements -join ',')) }
        }
        $exec = @($xml.Task.Actions.Exec)[0]
        $principalElements = @()
        foreach ($child in $xml.Task.Principals.ChildNodes) {
            if ($child.NodeType -ne 'Element') { continue }
            $principalElements += $child
        }
        if ($principalElements.Count -ne 1) {
            return @{ State = 'mismatch'; Reason = ('principals=' + $principalElements.Count) }
        }
        $principal = $principalElements[0]
        # §210.3 W1: the task identity is USER-scoped - a GroupId-only
        # (group-principal) export is NEVER treated as a User principal.
        $principalUserId = [string]$principal.UserId
        if (-not $principalUserId) {
            return @{ State = 'mismatch'; Reason = ('principal has no UserId (group principal refused): ' + [string]$principal.GroupId) }
        }
        $triggerElements = @()
        if ($xml.Task.Triggers) {
            $triggerElements = @($xml.Task.Triggers.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
        }
        if ($trigger -eq 'ONDEMAND') {
            if ($triggerElements.Count -ne 0) {
                return @{ State = 'mismatch'; Reason = ('triggers=' + (($triggerElements | ForEach-Object { $_.LocalName }) -join ',')) }
            }
        }
        $expectedCommand = [string]$task.Command
        if ([string]$exec.Command -ne $expectedCommand) {
            return @{ State = 'mismatch'; Reason = ('command=' + [string]$exec.Command) }
        }
        if ([string]$exec.Arguments) {
            return @{ State = 'mismatch'; Reason = ('arguments=' + [string]$exec.Arguments) }
        }
        if ([string]$exec.WorkingDirectory) {
            # Both experiment task definitions carry NO WorkingDirectory (the
            # wrapper pins its own directory) - a workdir means a different
            # definition, never adopted.
            return @{ State = 'mismatch'; Reason = ('workingDirectory=' + [string]$exec.WorkingDirectory) }
        }
        $expectedPrincipal = [string]$task.IdentitySid
        # §210.3 W1: resolve the EXPORTED principal (a SID literal or an
        # account name) to a canonical SID through the real identity edge,
        # then compare SID to SID. Resolution failure/ambiguity is
        # unconfirmed; a resolved-but-different SID is a mismatch.
        $resolved = & $IdentityResolver $principalUserId
        if (-not $resolved.ok) {
            return @{ State = 'unconfirmed'; Reason = ('principal resolution failed/ambiguous (' + $principalUserId + '): ' + [string]$resolved.reason) }
        }
        if ([string]$resolved.sid -cne $expectedPrincipal) {
            return @{ State = 'mismatch'; Reason = ('principal=' + [string]$resolved.sid + ' (expected ' + $expectedPrincipal + ')') }
        }
        # §97.2: the logon context and its implied RunLevel are enforced
        # from the SINGLE resolved source ($expectedLogon = the spec,
        # cross-checked by the param) - (Password) is a different security
        # context than the blocked (S4U); no implicit skip path remains.
        $expectedRunLevel = if ($expectedLogon -eq 'Password') { 'LeastPrivilege' } else { 'HighestAvailable' }
        # §210 W2: Task Scheduler OMITS <LogonType> on this host when the task
        # runs as a WELL-KNOWN SERVICE principal with the S4U logon (SYSTEM has
        # no Password alternative - the effective logon context is unambiguously
        # S4U; the OS-imposed default). An ABSENT LogonType is therefore
        # accepted ONLY for that exact case; a Password task (or any other
        # expected logon) with an absent LogonType still refuses - the logon
        # context is security-critical and never implied for interactive tasks.
        $logonText = [string]$principal.LogonType
        $systemS4uImplied = ($logonText -eq '' -and $expectedLogon -eq 'S4U' -and $resolved.ok -and $resolved.sid -eq 'S-1-5-18')
        if (-not $systemS4uImplied -and $logonText -ne $expectedLogon) {
            return @{ State = 'mismatch'; Reason = ('logonType=' + $logonText) }
        }
        # §210 W2: Task Scheduler OMITS <RunLevel> from the schtasks /XML
        # re-export on this host even for an explicitly registered run level,
        # while the in-process definition read keeps it. An ABSENT RunLevel
        # takes the OS-imposed default of the (already matched) logon type
        # (Password -> LeastPrivilege, S4U -> HighestAvailable) - semantically
        # the SAME security context the verifier requires. An explicit
        # DIFFERENT value still refuses; nothing is inferred from a foreign
        # logon (the LogonType check above already ran).
        $runLevelText = [string]$principal.RunLevel
        if ($runLevelText) {
            if ($runLevelText -cne $expectedRunLevel) {
                return @{ State = 'mismatch'; Reason = ('runLevel=' + $runLevelText) }
            }
        }
        if ($trigger -eq 'ONDEMAND') {
            # R62-D: the registered definition must still be legally
            # on-demand - no automatic trigger AND AllowStartOnDemand enabled.
            # §210 W2: Task Scheduler OMITS AllowStartOnDemand on this host's
            # schtasks /XML re-export too; an ABSENT value is the OS default
            # (true) - the SAME on-demand semantics. An explicit present value
            # that is NOT 'true' refuses.
            $settings = $xml.Task.Settings
            $allowProp = if ($null -ne $settings) { $settings.PSObject.Properties['AllowStartOnDemand'] } else { $null }
            $allowValue = ''
            if ($null -ne $allowProp) { $allowValue = [string]$allowProp.Value }
            if ($allowValue -and $allowValue -cne 'true') {
                return @{ State = 'mismatch'; Reason = 'AllowStartOnDemand is not true' }
            }
        }
        return @{ State = 'owned'; Command = [string]$exec.Command; Principal = $principalUserId }
    } catch {
        return @{ State = 'unconfirmed'; Reason = ("definition unparsable: " + $_.Exception.Message) }
    }
}

function Confirm-D4TaskIdentity {
    # R62-C: FULL definition identity against the EXPECTED definition for
    # this inventory entry - reads the exported definition through the
    # schtasks boundary and hands it to the SHARED shape verifier (§95.5).
    param([hashtable]$Task, [string]$Trigger,
          [scriptblock]$SchtasksRunner, [string]$LogonType = '')
    $query = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Query', '/TN', $task.Name, '/XML')
    $text = (@($query.Output) -join "`n")
    if ($query.ExitCode -ne 0) {
        if ($text -match 'cannot find|找不到') {
            return @{ State = 'absent' }
        }
        return @{ State = 'unconfirmed'; Reason = ("query failed (exit " + $query.ExitCode + ")") }
    }
    return (Test-D4TaskDefinitionShape -DefinitionXml $text -Task $Task -Trigger $Trigger -LogonType $LogonType)
}

function Stop-D4TaskObject {
    # R62-C: stop+delete ONLY after the FULL identity check AND the broker
    # process exit confirmation. A mismatched definition (foreign wrapper,
    # extra ComHandler, wrong principal, extra arguments) NEVER receives
    # /End or /Delete. The final fact is the process being gone.
    param([hashtable]$Task, [string]$Trigger,
          [scriptblock]$SchtasksRunner, [scriptblock]$ProcessEvidenceRunner)
    # The expected principal travels INSIDE the Task record (IdentitySid);
    # Confirm-D4TaskIdentity reads the FULL definition from the scheduler.
    # The expected logon type travels with it too when present (empty = no
    # logon check for call sites that never carried it).
    $identity = Confirm-D4TaskIdentity -Task $Task -Trigger $Trigger `
        -SchtasksRunner $SchtasksRunner -LogonType ([string]$task.LogonType)
    if ($identity.State -eq 'absent') {
        Write-Log ("experiment task already absent (idempotent): " + $task.Name)
        return @{ Stopped = $true; Deleted = $false; Reason = 'absent' }
    }
    if ($identity.State -ne 'owned') {
        throw ("experiment task identity NOT confirmed (state=" + $identity.State +
               " reason=" + $identity.Reason + "); refusing to stop/delete")
    }
    $end = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/End', '/TN', $task.Name)
    if ($end.ExitCode -ne 0) {
        throw ("experiment task /End failed (exit " + $end.ExitCode + "); cleanup stops, evidence kept")
    }
    # Exit confirmation: the task's process evidence must show the image
    # gone BEFORE the definition is deleted (a deleted task must never
    # outlive a running broker).
    $evidence = Get-D4ProcessEvidence -ImageLeaf (Split-Path $task.Command -Leaf) `
        -ExperimentRoot $Task.Root -Runner $ProcessEvidenceRunner
    if ($evidence.Unconfirmed) {
        throw ("process evidence unconfirmed after /End; refusing to delete the task definition (cleanup stops, evidence kept)")
    }
    if ($evidence.Pids.Count -gt 0) {
        throw ("experiment process still alive after /End (pids " + ($evidence.Pids -join ',') +
               "); refusing to delete the task definition")
    }
    $del = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Delete', '/TN', $task.Name, '/F')
    if ($del.ExitCode -ne 0) {
        throw ("experiment task delete failed (exit " + $del.ExitCode + "); cleanup stops, evidence kept")
    }
    return @{ Stopped = $true; Deleted = $true; Reason = 'identity verified + process exit confirmed' }
}

function Get-D4ProcessEvidence {
    # Process evidence by ACTUAL image path, structured (Unconfirmed/Pids)
    # exactly like the reviewed Get-BrokerProcessPids - a failed query is
    # NEVER "no process". The match is CLIENT-SIDE against the image name in
    # SINGLE QUOTES: a WQL -Filter (Name='...') and even double-quoted
    # client literals are destroyed by the runner's -Command pass-through on
    # this host (empirically: 'invalid query' / 'unexpected token'), so the
    # whole table is read and filtered in-process - only the leaf name needs
    # quoting and single quotes survive the chain.
    param([string]$ImageLeaf, [string]$ExperimentRoot, [scriptblock]$Runner)
    $script = '$ErrorActionPreference = ''Stop''; try { ' +
              'Get-CimInstance Win32_Process | Where-Object { $_.Name -eq ''' + $ImageLeaf + ''' } | ' +
              'ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; CommandLine = $_.CommandLine; ExecutablePath = $_.ExecutablePath } } | ' +
              'ConvertTo-Json -Compress } catch { exit 1 }'
    $result = Invoke-CommandRunner -Runner $Runner -ArgumentList @(
        'pwsh', '-NoProfile', '-Command', $script)
    if ($result.ExitCode -ne 0) {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    $stdout = (@($result.Output) -join "`n").Trim()
    if (-not $stdout) {
        return @{ Unconfirmed = $false; Pids = @(); Rows = @() }
    }
    try {
        $payload = $stdout | ConvertFrom-Json
    } catch {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    if ($null -eq $payload -or $payload -is [bool]) {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    $rows = @($payload)
    $pids = @()
    foreach ($row in $rows) {
        $imagePath = [string]$row.ExecutablePath
        if ($imagePath -and $imagePath.StartsWith($ExperimentRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            $pids += [int]$row.ProcessId
        }
    }
    return @{ Unconfirmed = $false; Pids = $pids; Rows = $rows }
}

function Get-D4ExperimentProcessEvidence {
    # §118.1: experiment process occupancy by ACTUAL image path OR command
    # line - the share wrapper runs under cmd.exe/python, so an ImageLeaf
    # name filter (Name='run-live.cmd') provably misses it. A failed or
    # unparseable query is UNCONFIRMED (never 'clean'). Matches only objects
    # that carry the EXPERIMENT ROOT in their image path or command line -
    # production processes are never matched by a broad name.
    param([string]$ExperimentRoot, [scriptblock]$Runner)
    $script = '$ErrorActionPreference = ''Stop''; try { ' +
              'Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; CommandLine = $_.CommandLine; ExecutablePath = $_.ExecutablePath } } | ' +
              'ConvertTo-Json -Compress } catch { exit 1 }'
    $result = Invoke-CommandRunner -Runner $Runner -ArgumentList @(
        'pwsh', '-NoProfile', '-Command', $script)
    if ($result.ExitCode -ne 0) {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    $stdout = (@($result.Output) -join "`n").Trim()
    if (-not $stdout) {
        return @{ Unconfirmed = $false; Pids = @(); Rows = @() }
    }
    try { $payload = $stdout | ConvertFrom-Json } catch {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    if ($null -eq $payload -or $payload -is [bool]) {
        return @{ Unconfirmed = $true; Pids = @(); Rows = @() }
    }
    $pids = @(); $hits = @()
    foreach ($row in @($payload)) {
        $img = [string]$row.ExecutablePath
        $cmd = [string]$row.CommandLine
        $isHit = (($img) -and $img.StartsWith($ExperimentRoot + '\', [StringComparison]::OrdinalIgnoreCase)) -or
                 (($cmd) -and $cmd.IndexOf($ExperimentRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
        if ($isHit) {
            $pids += [int]$row.ProcessId
            $hits += [pscustomobject]@{ pid = [int]$row.ProcessId; image = $img; cmd = $cmd }
        }
    }
    return @{ Unconfirmed = $false; Pids = $pids; Rows = $hits }
}

function ConvertTo-D4WfpDumpText {
    # §130/§132/§136/§137: turns the REAL netsh WFP dump line array into a
    # single parseable text, repairing ONLY the two documented wrap patterns.
    # (1) netsh wraps ONE start-tag NAME strictly inside <serviceName>
    # (`<se<CRLF>rviceName>`, the MPSSVC record; sha256 c533bb14...). (2)
    # netsh has also wrapped a provider record's tag-open as `<<CRLF>flags/>`
    # (single occurrence in the 2026-09-11-18:33 live capture).
    # UmD4WfpDumpNormalizer repairs exactly those splits (open tag, each at
    # most once per document; for (1) both fragments non-empty and the
    # joined name === "serviceName"; for (2) an EMPTY name accumulator, the
    # fragment EXACTLY "flags" and the SELF-CLOSING `/>` suffix) and leaves
    # EVERYTHING else byte-for-byte: other tag-name splits, text content,
    # quoted attribute values, attribute-area line breaks and the `<?xml...?>`
    # PI are all preserved. A `<!` construct (comment/CDATA/DOCTYPE - the
    # dump has NONE, verified) is explicitly REJECTED with a NUL sentinel,
    # so the output can never parse: an unexpected shape stays UNCONFIRMED
    # and is never rewritten into a legal-looking document. The
    # original-dump summary + repair/reject outcome are exposed as
    # UmD4WfpDumpNormalizer static fields (never the raw rules). The
    # reviewed parser Get-D4WfpRuleEvidence is NOT modified and still
    # requires a fully valid document; a dump that fails after normalization
    # stays UNCONFIRMED - nothing is silently accepted.
    param([object[]]$Lines)
    if (-not ([System.Management.Automation.PSTypeName]'UmD4WfpDumpNormalizer').Type) {
        Add-Type -TypeDefinition @"
using System.Text;
// R132-A/R136-A/R137-B: the WFP dump normalizer repairs ONLY the two
// DOCUMENTED netsh wrap patterns (each at most once per document, OPEN tags
// only):
//  (1) a line break INSIDE a tag NAME: `<se<CRLF>rviceName>` (the MPSSVC
//      record; two elevated captures share sha256 c533bb14...) - repaired
//      only when the full gate holds: no whitespace/attribute seen yet in
//      the name, both fragments non-empty, joined name EXACTLY "serviceName".
//  (2) a line break BETWEEN '<' and the tag name: `<<CRLF>flags/>` (one
//      occurrence observed in the 2026-09-11-18:33 live dump, a provider
//      record). R137: the gate is confined to the EXACT observed shape -
//      the name accumulator is EMPTY, the fragment after the break is
//      EXACTLY "flags", AND the tag is self-closing (`/>` immediately after
//      the name). Any other name (filters/filterKey/item/wfpdiag/...), an
//      attribute start, a case variant, or a plain non-self-closing open
//      form stays byte-for-byte so the parser still rejects the broken
//      tag-open (UNCONFIRMED). A generic legal-name join is NOT made: it
//      could let a damaged `<filters>` be fabricated into a legal empty set
//      (an ABSENT-looking verdict), which violates the review contract.
// Every other name split, a second occurrence of either pattern, and
// closing-tag wraps stay byte-for-byte so the parser still sees - and
// rejects - the original bad structure. Line breaks in TEXT, inside quoted
// attribute values, after the first whitespace of a tag (legal XML S) and
// inside <?...?> are always preserved verbatim. A '<!' construct
// (comment/CDATA/DOCTYPE - the captured dumps contain none, verified) is
// explicitly REJECTED: the offending '<' is replaced by a NUL byte, which
// XML 1.0 forbids everywhere, so the document can never parse and the
// reviewed parser reports UNCONFIRMED. A would-be repair must never
// fabricate success from an unverified shape.
public static class UmD4WfpDumpNormalizer {
    // Repair diagnostics as UmD4RuleCodec-style static fields: a summary of
    // the ORIGINAL dump (never its content) plus the repair/reject outcome.
    // Reset at the start of every Normalize call.
    public static int OriginalChars = 0;
    public static int OriginalLines = 0;
    public static bool BoMStripped = false;
    public static int RepairCount = 0;        // total repairs this call
    public static int RepairServiceCount = 0; // in-name <serviceName> joins
    public static int RepairTagOpenCount = 0; // pre-name '<'+CRLF joins
    public static string RepairReason = null;
    public static bool Rejected = false;
    public static string RejectReason = null;
    public static string Summary = null;

    public static string Normalize(string text) {
        Reset();
        if (string.IsNullOrEmpty(text)) {
            Summary = "empty input; no repairs, no rejections";
            return text;
        }
        // A leading U+FEFF (BOM) in a captured line array would make the very
        // first character illegal for XmlDocument ("data at the root level is
        // invalid"); PS 5.1 native capture already strips it, this guards the
        // file-based/byte-decode capture paths. Only a LEADING BOM is stripped.
        int start = 0;
        while (start < text.Length && text[start] == '\uFEFF') { start++; }
        BoMStripped = start > 0;
        OriginalChars = text.Length;
        OriginalLines = 1;
        for (int t = start; t < text.Length; t++) { if (text[t] == '\n') { OriginalLines++; } }
        StringBuilder sb = new StringBuilder(text.Length - start + 16);
        bool inTag = false;   // inside a start/end tag '<...>'
        bool inPi = false;    // inside '<?...?>'
        bool closing = false; // the current tag is '</...>'
        bool nameOnly = true; // still inside the tag NAME (no whitespace yet)
        int tagStart = -1;    // sb offset of the current tag's '<'
        char quote = '\0';
        const int MaxRepairs = 1;
        int repairedService = 0;
        int repairedOpen = 0;
        bool rejected = false;
        string rejectReason = null;
        for (int i = start; i < text.Length; i++) {
            char c = text[i];
            if (!inTag && !inPi) {
                if (c != '<') { sb.Append(c); continue; }
                if (i + 1 < text.Length && text[i + 1] == '?') {
                    inPi = true; sb.Append(c); continue;
                }
                if (i + 1 < text.Length && text[i + 1] == '!') {
                    // '<!' can only open a comment, a CDATA section or a
                    // DOCTYPE - none verified in the captured dump. REJECT:
                    // the NUL byte is illegal in XML 1.0 everywhere, so the
                    // remainder can never parse and the parser reports
                    // UNCONFIRMED. The construct is never rewritten into a
                    // legal-looking document.
                    rejected = true;
                    rejectReason = "unsupported '<!' construct (comment/CDATA/DOCTYPE) at input offset " + i;
                    sb.Append('\u0000');
                    sb.Append(text, i + 1, text.Length - i - 1);
                    break;
                }
                inTag = true;
                closing = (i + 1 < text.Length && text[i + 1] == '/');
                nameOnly = true;
                quote = '\0';
                tagStart = sb.Length;
                sb.Append(c);
                continue;
            }
            if (inPi) {
                sb.Append(c);
                if (c == '>' && i > 0 && text[i - 1] == '?') { inPi = false; }
                continue;
            }
            // inTag
            if (quote != '\0') {
                if (c == quote) { quote = '\0'; }
                sb.Append(c);
                continue;
            }
            if (c == '"' || c == '\'') { quote = c; sb.Append(c); continue; }
            if (c == '>') { inTag = false; sb.Append(c); continue; }
            if (c == '\r' || c == '\n') {
                if (nameOnly) {
                    // Line breaks inside the tag-OPEN area. Only the two
                    // DOCUMENTED netsh wrap patterns are repaired (open
                    // tags, each at most once per document); every other
                    // break stays byte-for-byte so the parser still sees
                    // the broken tag-open and rejects it.
                    if (!closing) {
                        string first = (tagStart >= 0 && sb.Length > tagStart + 1)
                            ? sb.ToString(tagStart + 1, sb.Length - tagStart - 1) : "";
                        int j = i;
                        while (j < text.Length && (text[j] == '\r' || text[j] == '\n')) { j++; }
                        int k = j;
                        while (k < text.Length && text[k] != '\r' && text[k] != '\n'
                               && text[k] != ' ' && text[k] != '\t' && text[k] != '\f'
                               && text[k] != '>' && text[k] != '"' && text[k] != '\'' && text[k] != '/') { k++; }
                        string second = text.Substring(j, k - j);
                        if (repairedService < MaxRepairs && first.Length > 0 && second.Length > 0
                            && string.Equals(first + second, "serviceName", System.StringComparison.Ordinal)) {
                            // documented repair (1): join the two name fragments
                            sb.Append(second);
                            repairedService++;
                            RepairReason = "joined one in-tag <serviceName> split at input offset " + i;
                            i = k - 1; // the loop ++ lands on the char after the name
                            continue;
                        }
                        // R137: repair (2) is confined to the EXACT observed
                        // self-closing shape `<<CRLF>flags/>`: the name
                        // fragment must be EXACTLY "flags" AND the tag must
                        // be self-closing (`/>` immediately after the name).
                        // Any other name, an attribute start, or a plain
                        // (non-self-closing) open form stays byte-for-byte so
                        // the parser rejects it (UNCONFIRMED) - a damaged
                        // `<filters>`/`<filterKey>` must never be fabricated
                        // into a legal empty/absent-looking structure.
                        if (repairedOpen < MaxRepairs && first.Length == 0
                            && string.Equals(second, "flags", System.StringComparison.Ordinal)
                            && k + 1 < text.Length && text[k] == '/' && text[k + 1] == '>') {
                            sb.Append(second);
                            repairedOpen++;
                            RepairReason = "joined one pre-name '<'+CRLF+'flags/>' wrap at input offset " + i;
                            i = k - 1; // the loop ++ lands on the char after the name
                            continue;
                        }
                    }
                    // not repaired: the break stays byte-for-byte
                }
                // attribute-area line breaks are legal XML whitespace (S) and
                // are also preserved verbatim (never rewritten to a space).
                sb.Append(c);
                continue;
            }
            if (c == ' ' || c == '\t' || c == '\f') { nameOnly = false; }
            sb.Append(c);
        }
        RepairCount = repairedService + repairedOpen;
        RepairServiceCount = repairedService;
        RepairTagOpenCount = repairedOpen;
        Rejected = rejected;
        RejectReason = rejectReason;
        Summary = "chars=" + OriginalChars + " lines=" + OriginalLines
            + " bomStripped=" + BoMStripped
            + " repaired=" + RepairCount + " rejected=" + Rejected;
        return sb.ToString();
    }

    private static void Reset() {
        OriginalChars = 0;
        OriginalLines = 0;
        BoMStripped = false;
        RepairCount = 0;
        RepairServiceCount = 0;
        RepairTagOpenCount = 0;
        RepairReason = null;
        Rejected = false;
        RejectReason = null;
        Summary = null;
    }
}
"@
    }
    $joined = (@($Lines) -join "`n")
    if ($joined.Length -eq 0) { return @() }
    $normalized = [UmD4WfpDumpNormalizer]::Normalize($joined)
    return @($normalized -split "`n")
}

function New-D4RealNetStatRunner {
    # §130/§143: the REAL native-capture runner over the full-argv contract
    # (argv[0] = the executable, the rest = the arguments). The native edge
    # is injectable so the offline tests run the REAL closure with a stub
    # launch; the exit code is read AFTER the native call.
    param([scriptblock]$Native)
    if (-not $Native) {
        $Native = {
            param([string]$Exe, [string[]]$Rest)
            # R143: with a CALLER ErrorActionPreference=Stop (the real entry
            # is Stop by contract), PS 5.1 turns a native stderr line under
            # the wrapper's 2>&1 into a TERMINATING NativeCommandError even
            # though the wrapper sets Continue - the preference does not
            # reach this nested scriptblock (reproduced: schtasks /Query of
            # an ABSENT task threw instead of returning the absence record).
            # Setting it HERE keeps an expected stderr message (schtasks
            # "cannot find/找不到" absence evidence, icacls diagnostics) as a
            # captured record: the exit code stays the verdict, the text
            # stays the evidence, and nothing throws.
            $ErrorActionPreference = 'Continue'
            & $Exe @Rest
        }
    }
    return {
        param([string[]]$ArgumentList)
        # PS 5.1: a native command writing stderr under 2>&1 with a caller's
        # ErrorActionPreference=Stop turns the merged line into a TERMINATING
        # NativeCommandError. Expected-absence evidence (schtasks cannot
        # find) must stay a captured record; the exit code stays the verdict.
        $ErrorActionPreference = 'Continue'
        $exe = @($ArgumentList)[0]
        $rest = @($ArgumentList | Select-Object -Skip 1)
        $out = & $Native $exe $rest 2>&1
        New-TestRunResult -ExitCode $LASTEXITCODE -Output @($out)
    }.GetNewClosure()
}

function New-D4RealWfpEvidenceRunner {
    # §130: the REAL wfp-dump runner. Consumes the SAME full-argv contract as
    # New-D4RealNetStatRunner (argv[0] = the executable, the rest = the
    # arguments) - the old driver closure re-invoked 'netsh' as the FIRST
    # argument of 'netsh.exe' (exit 1, "cannot find command") and read
    # $LASTEXITCODE before the dump ran. The native edge is injectable so the
    # offline tests run the REAL closure with a stub launch. stderr NEVER
    # enters the Output (an error line would masquerade as XML); the dump is
    # normalized via ConvertTo-D4WfpDumpText (narrowed to the documented
    # <serviceName> start-tag split; every other malformed shape stays
    # byte-for-byte) so the reviewed parser (which is unchanged) receives a
    # valid document for the KNOWN dump shape and UNCONFIRMED for anything
    # else.
    #
    # ENCODING (W2 run17): netsh wfp writes its dump as UTF-8 WITH BOM; the
    # console pipeline on this host decodes native stdout through the system
    # codepage (GBK) and the BOM/UTF-8 bytes are destroyed before any
    # normalization can run - [xml] then fails with a phantom '</it>' tag
    # mismatch at ~line 148k. The runner therefore captures the dump through
    # a TEMP FILE written by netsh itself (UTF-8 BOM preserved) and reads it
    # back via [IO.File]::ReadAllLines (BOM-aware), never through the console
    # stdout. The file is removed before returning. With an injected Native,
    # the same file route is used (the stub writes the file).
    param([scriptblock]$Native)
    if (-not $Native) {
        $Native = {
            param([string]$Exe, [string[]]$Rest, [string]$DumpFile)
            # The dump must be written by netsh itself (UTF-8 BOM); replace
            # the 'file=-' stdout marker with the temp path.
            $args2 = @($Rest | ForEach-Object { if ($_ -eq 'file=-') { 'file=' + $DumpFile } else { $_ } })
            & $Exe @args2 2>$null | Out-Null
        }
    }
    return {
        param([string[]]$ArgumentList)
        $ErrorActionPreference = 'Continue'
        $nativeInjected = ($null -ne $Native)
        $exe = @($ArgumentList)[0]
        $rest = @($ArgumentList | Select-Object -Skip 1)
        $dumpFile = [IO.Path]::GetTempFileName()
        try {
            if ($rest -contains 'file=-') {
                & $Native $exe $rest $dumpFile
                if (-not (Test-Path -LiteralPath $dumpFile)) {
                    return (New-TestRunResult -ExitCode 1 -Output @('wfp dump file missing'))
                }
            } else {
                # A caller-supplied file path is honored as-is (no rewrite).
                & $Native $exe $rest ''
            }
            $out = @()
            if (Test-Path -LiteralPath $dumpFile) {
                $out = @([IO.File]::ReadAllLines($dumpFile))
            }
            # The dump was captured through the file; netsh's own exit is
            # authoritative only when a native command actually ran. With an
            # injected Native (offline), the file route's success IS the
            # verdict: the file exists and was read.
            $exit = if (-not $nativeInjected) { $LASTEXITCODE } else { 0 }
            New-TestRunResult -ExitCode $exit -Output (ConvertTo-D4WfpDumpText -Lines $out)
        } finally {
            if (Test-Path -LiteralPath $dumpFile) { Remove-Item -LiteralPath $dumpFile -Force -ErrorAction SilentlyContinue }
        }
    }.GetNewClosure()
}

function New-D4RealPipeEvidenceRunner {
    # §130: the REAL named-pipe enumeration edge. Enumerates the Win32 device
    # namespace DIRECTORY '\\.\pipe\' (literal; PS single quotes carry no
    # escapes - the old driver string '\.\pipe' resolved to 'C:\pipe' and
    # threw). The enumeration edge is injectable so tests assert the exact
    # directory string; a throwing edge keeps propagating (the caller turns it
    # into pipe-enumeration-UNCONFIRMED, never a silent pass).
    param([scriptblock]$EnumEdge)
    if (-not $EnumEdge) {
        $EnumEdge = { param([string]$Path) return @([System.IO.Directory]::GetFiles($Path)) }
    }
    return {
        param()
        return (& $EnumEdge '\\.\pipe\')
    }.GetNewClosure()
}

function Get-D4WfpRuleEvidence {
    # §118.1/§120.1: STRUCTURED WFP filter occupancy - runs the dump via the
    # injectable runner, requires exit 0 AND a document matching the REAL
    # wfpdiag schema (root <wfpdiag>, a <filters> collection whose every
    # <item> carries a <filterKey> GUID). Only a VERIFIED COMPLETE collection
    # yields per-rule PRESENT/ABSENT; a wrong root, a missing/no <filters>
    # collection, a malformed record, non-XML garbage, empty output or a
    # non-zero exit is UNCONFIRMED - never 'clean'.
    param([scriptblock]$Runner, [string[]]$RuleIds)
    $fail = { param([string]$reason) return @{ Unconfirmed = $true; Present = @(); Absent = @(); Reason = $reason } }
    # The dump MUST be captured through the real WFP runner (temp-file + BOM
    # normalization) - a bare netsh through the runner-less path decodes the
    # UTF-8 BOM dump via the console codepage and [xml] fails with a phantom
    # tag mismatch (W2 run17). $null means "use the reviewed real runner".
    if (-not $Runner) { $Runner = New-D4RealWfpEvidenceRunner }
    $result = Invoke-CommandRunner -Runner $Runner -ArgumentList @(
        'netsh', 'wfp', 'show', 'filters', 'file=-')
    if ($result.ExitCode -ne 0) {
        return (& $fail ('wfp dump failed (exit ' + $result.ExitCode + ')'))
    }
    $raw = (@($result.Output) -join "`n").Trim()
    if (-not $raw) {
        return (& $fail 'wfp dump returned empty output')
    }
    $doc = $null
    try { $doc = [xml]$raw } catch {
        return (& $fail 'wfp dump is NOT a parseable XML document (bad output)')
    }
    $root = $doc.DocumentElement
    if ($null -eq $root -or $root.LocalName -ne 'wfpdiag') {
        return (& $fail ('wfp dump root is NOT the WFP filter dump (wfpdiag): ' + $(if ($null -eq $root) { 'no root' } else { $root.LocalName })))
    }
    # §122: EXACTLY ONE <filters> collection at the root - a second collection
    # would make the set ambiguous (last-wins could mask an earlier target).
    $filtersNodes = @()
    foreach ($c0 in @($root.ChildNodes)) {
        if ($c0.NodeType -eq [System.Xml.XmlNodeType]::Element -and $c0.LocalName -eq 'filters') { $filtersNodes += $c0 }
    }
    if ($filtersNodes.Count -eq 0) {
        return (& $fail 'wfp dump has NO <filters> collection (not a filter document)')
    }
    if ($filtersNodes.Count -gt 1) {
        return (& $fail ('wfp dump carries MULTIPLE <filters> collections (' + $filtersNodes.Count + '); ambiguous, no verdict'))
    }
    $filtersNode = $filtersNodes[0]
    # The requested ids and the document keys are normalized: braces/case
    # stripped and the value must PARSE as a real GUID - an illegal non-empty
    # key can neither fabricate PRESENT nor wrongly prove ABSENT.
    $guidOk = { param([string]$text) $g = [Guid]::Empty; return [System.Guid]::TryParse(([string]$text).Trim('{', '}', ' '), [ref]$g) }
    # Every requested id must itself be a legal GUID; if not, the request set
    # is malformed and no occupancy verdict may be produced.
    $want = @{}
    foreach ($rid in @($RuleIds)) {
        if (-not (& $guidOk ([string]$rid))) {
            return (& $fail ('the requested rule id is not a legal GUID: ' + [string]$rid))
        }
        $want[([string]$rid).Trim('{', '}', ' ').ToLowerInvariant()] = $true
    }
    $have = @{}
    $recordsChecked = 0
    foreach ($item in @($filtersNode.ChildNodes)) {
        if ($item.NodeType -ne [System.Xml.XmlNodeType]::Element) { continue }
        if ($item.LocalName -ne 'item') {
            return (& $fail ('unexpected child element inside <filters>: ' + $item.LocalName))
        }
        $fks = @()
        foreach ($cc in @($item.ChildNodes)) {
            if ($cc.NodeType -eq [System.Xml.XmlNodeType]::Element -and $cc.LocalName -eq 'filterKey') { $fks += $cc }
        }
        if ($fks.Count -ne 1) {
            return (& $fail ('a <filters> record must carry EXACTLY ONE filterKey (got ' + $fks.Count + '); ambiguous, no verdict'))
        }
        $keyText = [string]$fks[0].InnerText
        if ([string]::IsNullOrWhiteSpace($keyText)) {
            return (& $fail 'a <filters> record carries an EMPTY filterKey (malformed record)')
        }
        if (-not (& $guidOk $keyText)) {
            return (& $fail ('a <filters> record filterKey is NOT a legal GUID: ' + $keyText))
        }
        $keyLow = $keyText.Trim('{', '}', ' ').ToLowerInvariant()
        if ($have.ContainsKey($keyLow)) {
            return (& $fail ('duplicate <filters> filterKey in the dump: ' + $keyText))
        }
        $have[$keyLow] = $true
        $recordsChecked++
    }
    $present = @(); $absent = @()
    foreach ($rid in @($RuleIds)) {
        $low = ([string]$rid).Trim('{', '}', ' ').ToLowerInvariant()
        if ($have.ContainsKey($low)) { $present += $rid } else { $absent += $rid }
    }
    return @{ Unconfirmed = $false; Present = $present; Absent = $absent
              Reason = ('wfp dump parsed (root wfpdiag, single <filters>, ' + $recordsChecked + ' unique GUID filter records)') }
}

function Test-D4CleanupComplete {
    # R62-C: the final verdict needs MANDATORY evidence. Empty process/port
    # evidence is legal ONLY when the collectors SUCCEEDED; a failed or
    # missing collector is unconfirmed and the cleanup CANNOT pass. Kept
    # foreign/shared objects are LISTED, never assumed gone.
    param([hashtable]$Inventory, [hashtable]$Contract, [hashtable]$Ledger,
          [string]$EvidenceDir,
          [scriptblock]$SchtasksRunner, [hashtable]$ProcessEvidence,
          [hashtable]$PortEvidence, [hashtable]$FileBoundary,
          [scriptblock]$RulesRunner)
    $missing = @()
    foreach ($t in $inventory['Tasks']) {
        $q = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
            'schtasks', '/Query', '/TN', $t.Name)
        $text = (@($q.Output) -join "`n")
        if ($q.ExitCode -eq 0) {
            $missing += ("task still present: " + $t.Name)
        } elseif (-not ($text -match 'cannot find|找不到')) {
            $missing += ("task query failed (not provably absent): " + $t.Name)
        }
    }
    if ($null -eq $ProcessEvidence) {
        $missing += "process evidence was NOT collected (unconfirmed; cleanup cannot pass)"
    } elseif ($ProcessEvidence['Unconfirmed']) {
        $missing += "process evidence query FAILED (unconfirmed)"
    } elseif ($ProcessEvidence['Pids'].Count -gt 0) {
        foreach ($p in $ProcessEvidence['Pids']) {
            $missing += ("experiment process still alive: pid=" + $p)
        }
    }
    if ($null -eq $PortEvidence) {
        $missing += "port evidence was NOT collected (unconfirmed; cleanup cannot pass)"
    } elseif ($PortEvidence['Unconfirmed']) {
        $missing += "port check failed (unconfirmed)"
    } elseif ($PortEvidence['Listening']) {
        $missing += "experiment port 9100 still has a listener"
    }
    if ($RulesRunner) {
        # The 'list' verb MUST receive the experiment rule ids: the runner
        # contract emits a PRESENT/ABSENT verdict line PER REQUESTED id - a
        # runner called with no ids can never produce a verdict for this
        # run's rules (absence needs explicit evidence, never an empty pass).
        $rules = Invoke-CommandRunner -Runner $RulesRunner -ArgumentList @(
            @('limits', 'list') + @($inventory['RuleIds']))
        if ($rules.ExitCode -ne 0) {
            $missing += ("limit rule query failed (exit " + $rules.ExitCode + "); rule state UNCONFIRMED")
        } else {
            # Fixed output contract: the runner emits "PRESENT <id>" /
            # "ABSENT <id>" lines for the experiment rule ids. An id with a
            # PRESENT line is still in place; an id MISSING from the output
            # is UNCONFIRMED (absence needs explicit evidence, never a bare
            # substring guess).
            $text = (@($rules.Output) -join "`n")
            foreach ($id in $inventory['RuleIds']) {
                if ($text -match ('(?m)^\s*PRESENT\s+' + [regex]::Escape($id) + '\s*$')) {
                    $missing += ("experiment limit rule still present: " + $id)
                } elseif (-not ($text -match ('(?m)^\s*ABSENT\s+' + [regex]::Escape($id) + '\s*$'))) {
                    $missing += ("limit rule state unconfirmed (no verdict line): " + $id)
                }
            }
        }
    } else {
        $missing += "no limit-rule collector supplied; rule state UNCONFIRMED"
    }
    # Ledger-driven file resolution: the ledger is the run's OWN ownership
    # record - every entry must be resolved or deliberately kept. 'removed'
    # must be gone; 'created' still present = never cleaned; 'failed-clean'
    # is a KEPT object that always blocks the pass; a 'planned' entry that
    # EXISTS was created outside this run's record (unknown takeover);
    # 'retained' is the R83 LEGAL terminal for dynamic artifacts deliberately
    # kept - its file being absent would mean it changed after retention
    # (reported, never silently passed). Task entries and the evidence
    # directory (the kept run record) are excluded.
    if ($null -eq $Ledger -or $null -eq $Ledger['entries']) {
        $missing += "intent ledger was NOT provided (owned objects cannot be verified)"
    } else {
        foreach ($e in $Ledger['entries']) {
            if ($e.kind -in 'task', 'rule', 'acl') { continue }   # resolved by their own flows
            if ($e.kind -eq 'dir' -and $EvidenceDir -and
                    $e.path.Equals($EvidenceDir, [StringComparison]::OrdinalIgnoreCase)) { continue }
            $exists = Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($e.path)
            if ($e.status -eq 'failed-clean') {
                $missing += ("kept failed-clean object (operator review): " + $e.path)
            } elseif ($e.status -eq 'removed' -and $exists) {
                $missing += ("removed object is STILL PRESENT: " + $e.path)
            } elseif ($e.status -eq 'created' -and $exists) {
                $missing += ("created object still present: " + $e.path)
            } elseif ($e.status -eq 'planned' -and $exists) {
                $missing += ("planned object unexpectedly present (unowned takeover?): " + $e.path)
            } elseif ($e.status -eq 'retained' -and -not $exists) {
                $missing += ("retained object is MISSING (changed since retention): " + $e.path)
            }
        }
    }
    return $missing
}

function Test-D4NetstatRowFacts {
    # §124: STRICT per-column field validation of a netstat -ano -p TCP row.
    # Splits on whitespace into EXACTLY five columns: TCP, LocalAddress,
    # ForeignAddress, a KNOWN TCP state and a numeric PID. Every address is
    # split at the colon and fully validated (IPv4 octets 0-255, bracketed
    # IPv6, legal *:* wildcard, numeric port 0-65535). Missing/extra
    # columns, an unknown state or an illegal address/port make the row NOT
    # legal - the caller then treats the whole output as UNCONFIRMED (a bad
    # row can never be swallowed by other legal rows).
    param([string]$Row)
    $toks = @([regex]::Split($Row, '\s+') | Where-Object { $_ -ne '' })
    if ($toks.Count -ne 5) {
        return @{ Legal = $false; Note = ('column count is ' + $toks.Count + ', expected 5') }
    }
    if ($toks[0] -cne 'TCP') {
        return @{ Legal = $false; Note = ('protocol is not TCP (the query is -p TCP): ' + $toks[0]) }
    }
    foreach ($af in @($toks[1], $toks[2])) {
        $ok = $false
        if ($af -ceq '*:*') {
            $ok = $true
        } elseif ($af -match '^\[([0-9A-Fa-f:\.]+)\]:(\d{1,5})$') {
            # §126: the bracketed part must PARSE as a real IPv6 address
            # ([System.Net.IPAddress]::TryParse + AddressFamily check - no
            # DNS/network call); the port range check is kept.
            $parsedIp = [System.Net.IPAddress]::None
            $prt = [int]$Matches[2]
            $ok = ([System.Net.IPAddress]::TryParse($Matches[1], [ref]$parsedIp) -and
                   $parsedIp.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 -and
                   $prt -ge 0 -and $prt -le 65535)
        } elseif ($af -match '^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$') {
            $o1 = [int]$Matches[1]; $o2 = [int]$Matches[2]
            $o3 = [int]$Matches[3]; $o4 = [int]$Matches[4]
            $prt = [int]$Matches[5]
            $ok = ($o1 -le 255 -and $o2 -le 255 -and $o3 -le 255 -and $o4 -le 255 -and $prt -le 65535)
        }
        if (-not $ok) {
            return @{ Legal = $false; Note = ('invalid address field: ' + $af) }
        }
    }
    $state = $toks[3].ToUpperInvariant()
    if ($state -notin @('LISTENING','ESTABLISHED','SYN_SENT','SYN_RECEIVED','FIN_WAIT_1','FIN_WAIT_2','TIME_WAIT','CLOSE_WAIT','CLOSING','LAST_ACK','CLOSED')) {
        return @{ Legal = $false; Note = ('unknown TCP state: ' + $toks[3]) }
    }
    $pidVal = 0
    # §126: PID must parse AND be NON-NEGATIVE (0 stays legal per netstat).
    if (-not [int]::TryParse($toks[4], [ref]$pidVal) -or $pidVal -lt 0) {
        return @{ Legal = $false; Note = ('invalid PID: ' + $toks[4]) }
    }
    $localPort9100 = (($toks[1] -match ':9100$') -and ($state -eq 'LISTENING'))
    return @{ Legal = $true; State = $state; Port9100Listening = $localPort9100 }
}
