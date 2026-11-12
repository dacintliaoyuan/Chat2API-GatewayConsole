# d4-account-bootstrap.ps1 - experiment account bootstrap machinery for the
# D4-X "password-managed" share logon plan (handoff §93, revised per §95).
# The experiment creates a DEDICATED local account (um_share_d4) with an
# auto-generated strong password, and registers the share task with that
# credential INSIDE ONE elevated process; the password never enters scripts,
# config, ledgers, logs, external argv, Git or chat. The production um_share
# account is never read, reset or touched.
#
# Offline-testable: every OS edge (account query/create/disable, the
# in-process task registration, the protected-store guard and the ACL
# probe) is an injectable runner; the PURE core here (password generator,
# state table, ownership, orchestration order) is what the offline suite
# drives with fake runners. NO OS call exists in this module outside the
# runner scriptblocks returned by New-D4AccountRunner,
# New-D4RealBootstrapStoreGuard and New-D4RealAclProbe, and dot-sourcing
# this file has zero side effects.
#
# Bootstrap state table (explicit unconfirmed - NEVER a fake rollback; every
# status write consults Test-D4BootstrapTransition):
#   absent -> creation-intent -> created(SID) -> task-registered | disabled
#   any failure with an unknown result -> unconfirmed (run stops)
#   created(SID) + task-registration failure -> unconfirmed (SID + occurred
#   actions retained for the authorized disable/keep decision)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# PURE: crypto-strong password generation + Windows password-policy fit.
# ---------------------------------------------------------------------------

function New-D4StrongPassword {
    # Crypto-random password (RNGCryptoServiceProvider - never Get-Random),
    # >= 16 chars with every required class present. Returns a SecureString;
    # the value is NEVER written anywhere by this function. Rejection
    # sampling avoids modulo bias on the full alphabet.
    [CmdletBinding()]
    param([int]$Length = 24)
    if ($Length -lt 16) { throw 'New-D4StrongPassword: length must be >= 16' }
    $classSets = @(
        'ABCDEFGHJKLMNPQRSTUVWXYZ'
        'abcdefghijkmnopqrstuvwxyz'
        '23456789'
        '!@#$%^&*()-_=+[]{},.:;?'
    )
    $all = $classSets -join ''
    $allLen = $all.Length
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try {
        $out = New-Object char[] $Length
        $rand = New-Object byte[] 2048
        $rng.GetBytes($rand)
        $ri = 0
        # Positions 0..3: one char from each required class (guarantee).
        for ($i = 0; $i -lt $classSets.Count; $i++) {
            $out[$i] = $classSets[$i][$rand[$ri] % $classSets[$i].Length]
            $ri++
        }
        # Remaining slots: uniform over the full alphabet with rejection
        # sampling (the top partial block of 256 % allLen is rejected).
        $maxPartial = [Math]::Floor(256 / $allLen) * $allLen
        for ($i = $classSets.Count; $i -lt $Length; ) {
            if ($ri -ge $rand.Length) { $rng.GetBytes($rand); $ri = 0 }
            $b = [int]$rand[$ri]
            $ri++
            if ($b -lt $maxPartial) {
                $out[$i] = $all[$b % $allLen]
                $i++
            }
        }
        return (ConvertTo-SecureString -String (-join $out) -AsPlainText -Force)
    } finally {
        $rng.Dispose()
    }
}

function Get-D4PasswordClassFacts {
    # PURE: character-class summary of a SecureString WITHOUT exposing the
    # value; returns @{ Length; HasUpper; HasLower; HasDigit; HasSymbol }.
    # The BSTR is zeroed before return.
    param([System.Security.SecureString]$Password)
    if ($null -eq $Password) { throw 'Get-D4PasswordClassFacts: password is required' }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try {
        $text = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        return @{
            Length = $text.Length
            HasUpper = ([regex]::IsMatch($text, '[A-Z]'))
            HasLower = ([regex]::IsMatch($text, '[a-z]'))
            HasDigit = ([regex]::IsMatch($text, '[0-9]'))
            HasSymbol = ([regex]::IsMatch($text, '[^A-Za-z0-9]'))
        }
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Test-D4PasswordPolicyFit {
    # PURE: does the described password satisfy the strong-password bar? The
    # VALUE is never passed here - only its class facts. Used by tests to
    # assert the generator's output qualifies without printing it.
    param([int]$Length, [bool]$HasUpper, [bool]$HasLower,
          [bool]$HasDigit, [bool]$HasSymbol)
    if ($Length -lt 16) { return $false }
    if (-not ($HasUpper -and $HasLower -and $HasDigit -and $HasSymbol)) {
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# Bootstrap state record (protected run-evidence location; NO secret fields).
# ---------------------------------------------------------------------------

function Get-D4BootstrapStatePath {
    # The bootstrap record binds the FULL run id (like the stage state) and
    # lives OUTSIDE the experiment root (S0's namespace check must never see
    # it as experiment content; §93.4 P1). The path is DERIVED ONLY HERE -
    # real mode never accepts an operator-chosen override (§95.3).
    param([hashtable]$Contract, [string]$RunId)
    [void](Test-D4RunIdFormat $RunId)
    $rel = $Contract['BootstrapStateRel'] -replace '<runid>', $RunId
    return (Join-Path $Contract['BootstrapRoot'] $rel)
}

$script:D4BootstrapStateFields = @(
    'schema', 'run_id', 'account_name', 'task_name', 'package_hash',
    'plan_identity', 'status',
    'created_sid', 'intent_at', 'created_at', 'task_registered_at',
    'unconfirmed_reason', 'task_register_result'
)

function Get-D4RuleRoleMap {
    # §99.3: the isolation triple is a ROLE→GUID mapping, never a bare GUID
    # set. Build it from EITHER the run plan's ordered triple
    # (index 0=v4-permit, 1=v4-block, 2=v6-block - the reviewed position
    # contract) OR the collected config's rule specs (layer/action derived).
    # Exactly one GUID per role, no unknown/duplicate roles, every value a
    # non-empty GUID.
    param([string[]]$OrderedIds, [object[]]$Rules)
    $map = @{}
    if ($OrderedIds) {
        if (@($OrderedIds).Count -ne 3) {
            throw ("Get-D4RuleRoleMap: the ordered triple needs exactly three GUIDs, got " + @($OrderedIds).Count)
        }
        $map['v4-permit'] = [string]$OrderedIds[0]
        $map['v4-block'] = [string]$OrderedIds[1]
        $map['v6-block'] = [string]$OrderedIds[2]
    } elseif ($Rules) {
        foreach ($r in @($Rules)) {
            $layer = if ($r -is [hashtable]) { [string]$r['layer'] } else { [string]$r.layer }
            $action = if ($r -is [hashtable]) { [string]$r['action'] } else { [string]$r.action }
            $id = if ($r -is [hashtable]) { [string]$r['id'] } else { [string]$r.id }
            $role = if ($layer -eq 'connect-v4' -and $action -eq 'permit') { 'v4-permit' }
                    elseif ($layer -eq 'connect-v4' -and $action -eq 'block') { 'v4-block' }
                    elseif ($layer -eq 'connect-v6' -and $action -eq 'block') { 'v6-block' }
                    else { throw ("Get-D4RuleRoleMap: an unknown rule role (layer/action '" + $layer + "'/'" + $action + "') refuses") }
            if ($map.ContainsKey($role)) {
                throw ("Get-D4RuleRoleMap: a duplicate role refuses: " + $role)
            }
            $map[$role] = $id
        }
        if ($map.Count -ne 3) {
            throw ("Get-D4RuleRoleMap: the rule set must cover exactly the three roles, got " + $map.Count)
        }
    } else {
        throw 'Get-D4RuleRoleMap: either -OrderedIds or -Rules is required'
    }
    foreach ($role in @('v4-permit', 'v4-block', 'v6-block')) {
        if (-not $map.ContainsKey($role)) {
            throw ("Get-D4RuleRoleMap: missing role: " + $role)
        }
        if (-not $map[$role]) {
            throw ("Get-D4RuleRoleMap: role " + $role + " carries an empty GUID")
        }
        try { [void][Guid]::Parse($map[$role]) } catch {
            throw ("Get-D4RuleRoleMap: role " + $role + " carries a non-GUID value: " + $map[$role])
        }
    }
    return $map
}

function Get-D4BootstrapPlanIdentity {
    # §97.4/§99.3: the normalized, IMMUTABLE plan fingerprint - the code
    # identity (package/closure) and the run-plan identity are SEPARATE
    # kinds. The fingerprint covers run/account/task + python home + the
    # runtime source dir + every runtime file hash + the RULE ROLES bound to
    # their GUIDs (a role→id canonical structure - sorting bare GUIDs would
    # erase the role binding and a swapped permit/block pair would look like
    # the same plan); it is bound into the record BEFORE creation-intent and
    # every same-run consumption re-checks it (a changed plan refuses before
    # any side effect; the random/unknown SID is never an input).
    param([string]$RunId, [string]$AccountName, [string]$TaskName,
          [string]$PythonHome, [string]$RuntimeSourceDir,
          [object[]]$RuntimeFiles, [hashtable]$RuleMap)
    [void](Test-D4RunIdFormat $RunId)
    if (-not $AccountName -or -not $TaskName -or -not $PythonHome -or -not $RuntimeSourceDir) {
        throw 'Get-D4BootstrapPlanIdentity: run/account/task/python home/runtime source are required'
    }
    if (-not $RuleMap -or $RuleMap.Count -eq 0) {
        throw 'Get-D4BootstrapPlanIdentity: the role→GUID rule map is required'
    }
    $rtParts = @()
    foreach ($rf in @($RuntimeFiles)) {
        if (-not $rf.file -or -not $rf.sha256) {
            throw 'Get-D4BootstrapPlanIdentity: every runtime file needs file+sha256'
        }
        $rtParts += ([string]$rf.file + '=' + [string]$rf.sha256)
    }
    $rtParts = @($rtParts | Sort-Object)
    $ruleParts = @()
    foreach ($role in @('v4-permit', 'v4-block', 'v6-block')) {
        if (-not $RuleMap.ContainsKey($role)) {
            throw ("Get-D4BootstrapPlanIdentity: the rule map misses the role: " + $role)
        }
        $ruleParts += ($role + ':' + ([string]$RuleMap[$role]).ToLowerInvariant())
    }
    if ($ruleParts.Count -ne 3) {
        throw ("Get-D4BootstrapPlanIdentity: exactly three role-bound rule GUIDs required, got " + $ruleParts.Count)
    }
    $canonical = (@(
        'run=' + $RunId,
        'account=' + $AccountName,
        'task=' + $TaskName,
        'python_home=' + $PythonHome,
        'runtime_source=' + $RuntimeSourceDir,
        'runtime_files=' + ($rtParts -join ';'),
        'rule_roles=' + ($ruleParts -join ';')
    ) -join '|')
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($canonical)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function New-D4BootstrapState {
    # Fresh record for a run (absent state; no SID yet, no secret ever).
    # package_hash binds the record to THIS code build; plan_identity binds
    # it to the IMMUTABLE run plan (§97.4) - both are mandatory.
    param([string]$RunId, [string]$AccountName, [string]$TaskName,
          [string]$PackageHash, [string]$PlanIdentity)
    if (-not $PackageHash) { throw 'New-D4BootstrapState: the package hash binding is required' }
    if (-not $PlanIdentity) { throw 'New-D4BootstrapState: the plan identity binding is required' }
    return @{
        schema = 'd4-account-bootstrap'
        run_id = $RunId
        account_name = $AccountName
        task_name = $TaskName
        package_hash = $PackageHash
        plan_identity = $PlanIdentity
        status = 'absent'
        created_sid = ''
        intent_at = ''
        created_at = ''
        task_registered_at = ''
        unconfirmed_reason = ''
        task_register_result = ''
    }
}

function Read-D4BootstrapState {
    # Validates the persisted STRUCTURE + the FULL binding set (run id,
    # account, task, package) + the exact field set; a damaged, foreign or
    # field-widened record refuses. Returns $null when absent.
    param([string]$StatePath, [string]$RunId, [string]$AccountName,
          [string]$TaskName, [string]$PackageHash, [string]$PlanIdentity,
          [hashtable]$FileBoundary)
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($StatePath))) {
        return $null
    }
    $raw = Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($StatePath)
    $state = $null
    try { $state = $raw | ConvertFrom-Json } catch {
        throw ("d4-account-bootstrap: bootstrap state corrupt: " + $_.Exception.Message)
    }
    if ($null -eq $state -or -not ($state -is [System.Management.Automation.PSCustomObject])) {
        throw ("d4-account-bootstrap: bootstrap state structure invalid: " + $StatePath)
    }
    # EXACT field set: unknown fields and missing fields both refuse (§95.3).
    $expectedFields = @($script:D4BootstrapStateFields) | Sort-Object
    $actualFields = @($state.PSObject.Properties.Name) | Sort-Object
    if ((($expectedFields -join '|')) -ne (($actualFields -join '|'))) {
        throw ("d4-account-bootstrap: bootstrap state field set invalid (expected " +
               ($expectedFields -join ',') + ", got " + ($actualFields -join ',') + "): " + $StatePath)
    }
    if ([string]$state.schema -ne 'd4-account-bootstrap') {
        throw ("d4-account-bootstrap: bootstrap state schema invalid: " + $StatePath)
    }
    # One binding check per owned slot - a drifted record never adopts.
    $bindings = @(
        @{ field = 'run_id'; expected = $RunId; label = 'run' },
        @{ field = 'account_name'; expected = $AccountName; label = 'account' },
        @{ field = 'task_name'; expected = $TaskName; label = 'task' },
        @{ field = 'package_hash'; expected = $PackageHash; label = 'package' },
        @{ field = 'plan_identity'; expected = $PlanIdentity; label = 'plan' }
    )
    foreach ($b in $bindings) {
        $actual = [string]$state.($b.field)
        if ($actual -ne [string]$b.expected) {
            throw ("d4-account-bootstrap: state belongs to " + $b.label + " '" + $actual +
                   "', not '" + [string]$b.expected + "'")
        }
    }
    if ([string]$state.status -notin 'absent', 'creation-intent', 'created',
            'task-registered', 'disabled', 'unconfirmed') {
        throw ("d4-account-bootstrap: invalid status in record: " + [string]$state.status)
    }
    return $state
}

function Save-D4BootstrapState {
    # Atomic write through the same boundary semantics as the stage state,
    # followed by a READ-BACK verification: a write that did not land as the
    # same record refuses (读写均验证, §95.3) instead of silently claiming a
    # persisted fact.
    param([string]$StatePath, [object]$State, [hashtable]$FileBoundary)
    $json = $State | ConvertTo-Json -Depth 6
    Invoke-D4File -Boundary $FileBoundary -Operation 'WriteText' -Arguments @($StatePath, $json)
    $readBack = [string](Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($StatePath))
    if ($readBack.Trim() -ne $json.Trim()) {
        throw ("d4-account-bootstrap: bootstrap state write did not read back identical (write verification failed): " + $StatePath)
    }
}

function Test-D4BootstrapTransition {
    # State machine: which transitions are legal. There is NO fake rollback
    # to a prior state; a failure with unknown result is unconfirmed. A
    # re-entry on an already-unconfirmed record may REFRESH the reason
    # (unconfirmed -> unconfirmed is legal - the state stays terminal); the
    # orchestration consults this table before EVERY status change (live
    # code, not documentation).
    param([string]$From, [string]$To)
    $legal = @{
        'absent'          = @('creation-intent', 'unconfirmed')
        'creation-intent' = @('created', 'unconfirmed')
        'created'         = @('task-registered', 'unconfirmed')
        'task-registered' = @('disabled', 'unconfirmed')
        'disabled'        = @('unconfirmed')
        'unconfirmed'     = @('unconfirmed')
    }
    if (-not $legal.ContainsKey($From)) { return $false }
    return ($To -in $legal[$From])
}


# ---------------------------------------------------------------------------
# §113 recovery record: same protected store, SEPARATE canonical path (the
# recovery record never overwrites the creation record - it BINDS the
# creation record's path + byte hash + the fixed SID instead), its own state
# machine, and the same validated read/bind semantics as the bootstrap state.
# ---------------------------------------------------------------------------

function Get-D4RecoveryStatePath {
    param([hashtable]$Contract, [string]$RunId)
    [void](Test-D4RunIdFormat $RunId)
    $rel = $Contract['RecoveryStateRel'] -replace '<runid>', $RunId
    return (Join-Path $Contract['BootstrapRoot'] $rel)
}

$script:D4RecoveryStateFields = @(
    'schema', 'run_id', 'account_name', 'task_name',
    'created_state_path', 'created_state_sha256', 'created_sid',
    'package_hash', 'plan_identity', 'status',
    'intent_at', 'reset_at', 'enabled_at', 'task_registered_at',
    'disabled_at', 'unconfirmed_reason', 'task_register_result'
)

function New-D4RecoveryState {
    param([string]$RunId, [string]$AccountName, [string]$TaskName,
          [string]$CreatedStatePath, [string]$CreatedStateSha256,
          [string]$CreatedSid, [string]$PackageHash, [string]$PlanIdentity)
    return [pscustomobject]@{
        schema = 'd4-account-recovery'; run_id = $RunId; account_name = $AccountName
        task_name = $TaskName
        created_state_path = $CreatedStatePath; created_state_sha256 = $CreatedStateSha256
        created_sid = $CreatedSid; package_hash = $PackageHash; plan_identity = $PlanIdentity
        status = 'absent'
        intent_at = ''; reset_at = ''; enabled_at = ''; task_registered_at = ''
        disabled_at = ''; unconfirmed_reason = ''; task_register_result = ''
    }
}

function Test-D4RecoveryTransition {
    # §113: the recovery state machine - same no-fake-rollback discipline as
    # the bootstrap machine. A re-entry on an interrupted attempt is REFUSED
    # by the orchestration (never auto-rotates); unconfirmed stays terminal.
    param([string]$From, [string]$To)
    $legal = @{
        'absent'          = @('recovery-intent', 'unconfirmed')
        'recovery-intent' = @('password-reset', 'unconfirmed')
        'password-reset'  = @('enabled', 'unconfirmed')
        'enabled'         = @('task-registered', 'unconfirmed')
        'task-registered' = @('disabled', 'unconfirmed')
        'disabled'        = @('unconfirmed')
        'unconfirmed'     = @('unconfirmed')
    }
    if (-not $legal.ContainsKey($From)) { return $false }
    return ($To -in $legal[$From])
}

function Read-D4RecoveryState {
    # Mirrors Read-D4BootstrapState for the recovery record: structure,
    # exact field set, schema, and the full binding set - a drifted/foreign/
    # damaged record refuses; $null when absent.
    param([string]$StatePath, [string]$RunId, [string]$AccountName,
          [string]$TaskName, [string]$PackageHash, [string]$PlanIdentity,
          [hashtable]$FileBoundary)
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($StatePath))) {
        return $null
    }
    $raw = Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($StatePath)
    $state = $null
    try { $state = $raw | ConvertFrom-Json } catch {
        throw ("d4-account-bootstrap: recovery state corrupt: " + $_.Exception.Message)
    }
    if ($null -eq $state -or -not ($state -is [System.Management.Automation.PSCustomObject])) {
        throw ("d4-account-bootstrap: recovery state structure invalid: " + $StatePath)
    }
    $expectedFields = @($script:D4RecoveryStateFields) | Sort-Object
    $actualFields = @($state.PSObject.Properties.Name) | Sort-Object
    if ((($expectedFields -join '|')) -ne (($actualFields -join '|'))) {
        throw ("d4-account-bootstrap: recovery state field set invalid (expected " +
               ($expectedFields -join ',') + ", got " + ($actualFields -join ',') + "): " + $StatePath)
    }
    if ([string]$state.schema -ne 'd4-account-recovery') {
        throw ("d4-account-bootstrap: recovery state schema invalid: " + $StatePath)
    }
    $bindings = @(
        @{ field = 'run_id'; expected = $RunId; label = 'run' },
        @{ field = 'account_name'; expected = $AccountName; label = 'account' },
        @{ field = 'task_name'; expected = $TaskName; label = 'task' },
        @{ field = 'package_hash'; expected = $PackageHash; label = 'package' },
        @{ field = 'plan_identity'; expected = $PlanIdentity; label = 'plan' }
    )
    foreach ($b in $bindings) {
        $actual = [string]$state.($b.field)
        if ($actual -ne [string]$b.expected) {
            throw ("d4-account-bootstrap: recovery state belongs to " + $b.label + " '" + $actual +
                   "', not '" + [string]$b.expected + "'")
        }
    }
    if ([string]$state.status -notin 'absent', 'recovery-intent', 'password-reset',
            'enabled', 'task-registered', 'disabled', 'unconfirmed') {
        throw ("d4-account-bootstrap: invalid recovery status in record: " + [string]$state.status)
    }
    return $state
}
# ---------------------------------------------------------------------------
# Protected bootstrap store (§95.3): the record location is FIXED (derived
# from the contract), its ancestors must be reparse-free, a pre-existing
# record must validate FULLY (an unsafe record is refused - never patched up
# by changing its ACL), and protected storage is established via the store
# guard BEFORE any account creation or task registration.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Trust gate (§97.3): "create protected storage" and "read-only verify an
# existing protected storage" are SEPARATE. The READ-ONLY gate proves the
# store is trusted BEFORE any record is adopted: reparse-free chain, owner
# SYSTEM/Administrators, and NO non-trusted principal holding a write-capable
# ACE on the root or the record leaf. An untrusted store is NEVER modified,
# NEVER adopted - zero ACL fixes, zero account/task actions, original bytes
# kept. The trusted-identity set is SID-stable (never localized names).
# ---------------------------------------------------------------------------

# Trusted owner identities, BARE + lowercase (the '*' ACE prefix is
# stripped before comparison; names cover the real Get-Acl forms).
$script:D4StoreTrustedOwnerSids = @('s-1-5-18', 's-1-5-32-544')
$script:D4StoreTrustedOwnerNames = @('nt authority\system', 'builtin\administrators')
# §99.2: the ONLY rights a foreign (non-SYSTEM/Admin) Allow ACE may hold on
# the protected store - the read/execute bit set (ReadAndExecute covers
# Read, ListDirectory and ReadPermissions as subsets). The decision works on
# RAW BITS: GA, Delete, DeleteChild, ChangePermissions, TakeOwnership,
# write/append bits, generic bits and ANY unknown bit all fail the subset
# test and refuse - "unknown" is never read-only.
$script:D4StoreSafeReadMask = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::ReadAndExecute -band 0xFFFFFFFF)

function Test-D4StoreOwnerTrusted([string]$Owner) {
    $o = ([string]$Owner).ToLowerInvariant()
    if (-not $o) { return $false }
    if ($o.StartsWith('*')) { $o = $o.Substring(1) }  # an ACE-style prefix is stripped defensively
    return ($o -in $script:D4StoreTrustedOwnerSids -or $o -in $script:D4StoreTrustedOwnerNames)
}
function Test-D4StoreIdentityTrusted([string]$Identity) {
    # an ACE identity is trusted when it IS the SYSTEM/Administrators SID
    # (or a name form of them - the offline fixtures use the SID forms)
    $i = ([string]$Identity).ToLowerInvariant()
    if (-not $i) { return $false }
    if ($i.StartsWith('*')) { return (Test-D4StoreOwnerTrusted $i.Substring(1)) }
    return (Test-D4StoreOwnerTrusted $i)
}

function Test-D4AclDescriptorTrusted {
    # §99.2: the ACL decision consumes a STRUCTURED descriptor record (the
    # shape the real Get-Acl adapter produces):
    #   @{ ok; ownerSid; rules = @( @{ identitySid; allow; rights(uint64) } ); error }
    # Contract (§99.2): owner must be SYSTEM/Administrators (SID form); the
    # rules collection must be NON-empty (an empty/unprovable DACL cannot
    # prove the store is usable AND protected - never inferred from zero
    # text lines); ANY Deny rule is conservatively refused; a foreign
    # Allow rule may hold ONLY read/execute bits - GenericAll(GA), Delete(D),
    # DeleteChild(DC), ChangePermissions, TakeOwnership, write/append bits
    # and ANY unknown bit are all refused ("unknown" is never read-only).
    param([hashtable]$Acl, [string]$Path)
    if ($null -eq $Acl -or $Acl.ok -ne $true) {
        $err = if ($null -ne $Acl) { [string]$Acl.error } else { 'no probe result' }
        return @{ trusted = $false; reason = ('ACL query untrusted for ' + $Path + ': ' + $err) }
    }
    $ownerSid = [string]$Acl.ownerSid
    if (-not (Test-D4StoreOwnerTrusted $ownerSid)) {
        return @{ trusted = $false; reason = ('owner is not SYSTEM/Administrators (SID) for ' + $Path + ': ' + $ownerSid) }
    }
    $rules = @($Acl.rules)
    if ($rules.Count -eq 0) {
        return @{ trusted = $false; reason = ('the structured descriptor carries NO ACE for ' + $Path +
                   ' - an empty DACL cannot prove the intended protection (never inferred from zero lines)') }
    }
    foreach ($rule in $rules) {
        if ($null -eq $rule -or -not $rule.identitySid -or $null -eq $rule.allow -or $null -eq $rule.rights) {
            return @{ trusted = $false; reason = ('unparsable/unknown ACE shape for ' + $Path + ': ' + ($rule | ConvertTo-Json -Compress)) }
        }
        if (-not $rule.allow) {
            # Conservative: Deny combinations are not simulated - refuse.
            return @{ trusted = $false; reason = ('a DENY ACE is present on ' + $Path + ' (identity ' +
                       [string]$rule.identitySid + '); Deny combinations are unsupported and refused') }
        }
        if (Test-D4StoreIdentityTrusted ([string]$rule.identitySid)) { continue }
        $foreign = [string]$rule.identitySid
        $rights = [uint64]$rule.rights
        if (($rights -band (-bnot [uint64]$script:D4StoreSafeReadMask)) -ne 0) {
            return @{ trusted = $false; reason = ('a non-SYSTEM/Administrators principal holds a write/delete/DACL/owner/generic ACE on ' +
                       $Path + ' (identity ' + $foreign + ', rights ' + $rights + ')') }
        }
    }
    return @{ trusted = $true; reason = '' }
}

function Test-D4BootstrapStoreTrust {
    # READ-ONLY trust gate shared by EVERY record consumer (bootstrap, S0/S1
    # ownShare exemption, T0 recovery precheck): reparse chain + owner +
    # write-ACE matrix on the root, and (when the record leaf exists) on the
    # leaf. Never modifies anything; a query failure is never trusted.
    param([string]$BootstrapRoot, [string]$StatePath,
          [scriptblock]$AclProbe, [scriptblock]$StoreProbe,
          [hashtable]$FileBoundary, [switch]$CheckLeaf)
    if (-not $AclProbe) {
        return @{ trusted = $false; reason = 'an ACL probe is required for the read-only trust gate (fail closed)' }
    }
    if (-not $StoreProbe) {
        return @{ trusted = $false; reason = 'a store probe is required for the read-only trust gate (fail closed)' }
    }
    $current = $BootstrapRoot
    while ($true) {
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
        $p = & $StoreProbe $current
        if ($null -ne $p -and ($p.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return @{ trusted = $false; reason = ('bootstrap ancestor is a reparse point: ' + $current) }
        }
    }
    $targets = @($BootstrapRoot)
    if ($CheckLeaf) { $targets += $StatePath }
    foreach ($target in $targets) {
        $p = & $StoreProbe $target
        if ($null -ne $p -and ($p.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return @{ trusted = $false; reason = ('bootstrap path is a reparse point: ' + $target) }
        }
    }
    $rootAcl = & $AclProbe $BootstrapRoot @{}
    $rootCheck = Test-D4AclDescriptorTrusted -Acl $rootAcl -Path $BootstrapRoot
    if (-not $rootCheck.trusted) { return $rootCheck }
    if ($CheckLeaf -and $null -ne $FileBoundary -and
            (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($StatePath))) {
        $leafAcl = & $AclProbe $StatePath @{}
        $leafCheck = Test-D4AclDescriptorTrusted -Acl $leafAcl -Path $StatePath
        if (-not $leafCheck.trusted) { return $leafCheck }
    }
    return @{ trusted = $true; reason = 'store trust verified (read-only)' }
}

function Test-D4BootstrapStoreReady {
    # §97.3: EXISTING store = read-only verification ONLY (never modified,
    # never adopted when untrusted); ABSENT store = protected creation via
    # the guard, then the SAME trust gate verifies the actual ACL (exit codes
    # never substitute for the permission facts). The record fields are
    # validated after the store itself is proven trusted.
    param([string]$StatePath, [string]$BootstrapRoot,
          [string]$RunId, [string]$AccountName, [string]$TaskName,
          [string]$PackageHash, [string]$PlanIdentity, [hashtable]$FileBoundary,
          [scriptblock]$StoreGuard, [scriptblock]$StoreProbe,
          [scriptblock]$AclProbe)
    if (-not $StoreProbe) {
        throw 'd4-account-bootstrap: a store probe (reparse/attributes) is required before any bootstrap action'
    }
    # 1. reparse-free ancestors of the bootstrap root.
    $current = $BootstrapRoot
    while ($true) {
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
        $probe = & $StoreProbe $current
        if ($null -ne $probe -and ($probe.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return @{ ok = $false; reason = ('bootstrap root ancestor is a reparse point: ' + $current) }
        }
    }
    $rootProbe = & $StoreProbe $BootstrapRoot
    if ($null -ne $rootProbe -and ($rootProbe.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        return @{ ok = $false; reason = ('bootstrap root is a reparse point: ' + $BootstrapRoot) }
    }
    # 2. EXISTING root: read-only trust verification - untrusted refuses with
    #    ZERO ACL modification and zero adoption.
    $rootExists = Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($BootstrapRoot)
    if ($rootExists) {
        $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $BootstrapRoot -StatePath $null `
            -AclProbe $AclProbe -StoreProbe $StoreProbe
        if (-not $trust.trusted) {
            return @{ ok = $false; reason = ('existing store is NOT trusted (zero modification, zero adoption): ' + $trust.reason) }
        }
    } else {
        # 2b. ABSENT root: protected creation via the guard, then the SAME
        #     gate verifies the ACTUAL ACL (never the icacls exit codes).
        if (-not $StoreGuard) {
            return @{ ok = $false; reason = 'protected store guard is required before any bootstrap action (fail closed)' }
        }
        try {
            $guard = & $StoreGuard $BootstrapRoot @{}
            if (-not $guard.ok) {
                return @{ ok = $false; reason = ('protected store establishment failed: ' + [string]$guard.error) }
            }
        } catch {
            return @{ ok = $false; reason = ('protected store guard threw: ' + $_.Exception.Message) }
        }
        $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $BootstrapRoot -StatePath $null `
            -AclProbe $AclProbe -StoreProbe $StoreProbe
        if (-not $trust.trusted) {
            return @{ ok = $false; reason = ('created store failed the trust verification: ' + $trust.reason) }
        }
    }
    # 3. a pre-existing record validates FULLY against this run's binding AND
    #    the LEAF carries a trusted DACL; an unsafe record refuses with zero
    #    patch-up.
    $recordState = $null
    if (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($StatePath)) {
        $leafTrust = Test-D4BootstrapStoreTrust -BootstrapRoot $BootstrapRoot -StatePath $StatePath `
            -AclProbe $AclProbe -StoreProbe $StoreProbe -FileBoundary $FileBoundary -CheckLeaf
        if (-not $leafTrust.trusted) {
            return @{ ok = $false; reason = ('existing record leaf is NOT trusted (zero modification, zero adoption): ' + $leafTrust.reason) }
        }
        try {
            $recordState = Read-D4BootstrapState -StatePath $StatePath -RunId $RunId -AccountName $AccountName `
                -TaskName $TaskName -PackageHash $PackageHash -PlanIdentity $PlanIdentity `
                -FileBoundary $FileBoundary
        } catch {
            return @{ ok = $false; reason = ('pre-existing bootstrap record is not a trusted record for this run (refused, never patched): ' + $_.Exception.Message) }
        }
    }
    $reason = if ($null -ne $recordState) {
        ('store trust verified + existing trusted record validated (' + [string]$recordState.status + ')')
    } else {
        'store trust verified (protected storage in place)'
    }
    return @{ ok = $true; reason = $reason }
}

function New-D4RealBootstrapStoreGuard {
    # REAL protected store guard (§97.3): an EXISTING directory is NEVER
    # modified - it must pass the read-only trust gate or the bootstrap
    # refuses. An ABSENT directory is created with minimal authorization
    # (inheritance removed, SYSTEM+Administrators only) through the native
    # runner, and the RESULT is verified through the same trust gate - the
    # icacls exit codes never substitute for the permission facts. The exact
    # production ACL semantics stay a real-machine verification item (D4-X).
    param([string]$RootDir, [scriptblock]$NativeRunner,
          [scriptblock]$AclProbe, [scriptblock]$StoreProbe,
          [hashtable]$FileBoundary)
    return {
        param([string]$Dir, [hashtable]$Ctx)
        try {
            $exists = Invoke-D4File -Boundary $FileBoundary -Operation 'Exists' -Arguments @($Dir)
            if ($exists) {
                $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $Dir -StatePath $null `
                    -AclProbe $AclProbe -StoreProbe $StoreProbe
                if (-not $trust.trusted) {
                    return @{ ok = $false; error = ('existing store is NOT trusted (zero modification): ' + $trust.reason) }
                }
                return @{ ok = $true; error = $null }
            }
            $null = Invoke-D4File -Boundary $FileBoundary -Operation 'MakeDir' -Arguments @($Dir)
            $inh = Invoke-CommandRunner -Runner $NativeRunner -ArgumentList @(
                'icacls.exe', $Dir, '/inheritance:r')
            if ($inh.ExitCode -ne 0) {
                return @{ ok = $false; error = ('icacls /inheritance:r failed (exit ' + $inh.ExitCode + '): ' + ($inh.Output -join ' ')) }
            }
            $grant = Invoke-CommandRunner -Runner $NativeRunner -ArgumentList @(
                'icacls.exe', $Dir, '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
            if ($grant.ExitCode -ne 0) {
                return @{ ok = $false; error = ('icacls grant failed (exit ' + $grant.ExitCode + '): ' + ($grant.Output -join ' ')) }
            }
            $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $Dir -StatePath $null `
                -AclProbe $AclProbe -StoreProbe $StoreProbe
            if (-not $trust.trusted) {
                return @{ ok = $false; error = ('created store failed the trust verification: ' + $trust.reason) }
            }
            return @{ ok = $true; error = $null }
        } catch {
            return @{ ok = $false; error = $_.Exception.Message }
        }
    }.GetNewClosure()
}

function New-D4RealAclProbe {
    # REAL ACL probe (§99.2): the ONLY OS access is the injectable Get-Acl
    # edge; everything else is STRUCTURED - the owner is translated to its
    # SID (never a localized display name) and the rules come from
    # GetAccessRules($true,$true,SecurityIdentifier), each carrying its
    # identity SID, Allow/Deny and the FULL FileSystemRights bit mask (the
    # decision consumes raw bits, so GA/D/DC and any unknown bit are judged
    # by value, not by letter codes). The offline suite replaces ONLY this
    # edge and drives the REAL decision/translation code.
    param([scriptblock]$GetAclEdge)
    return {
        param([string]$Path, [hashtable]$Ctx)
        try {
            $sd = & $GetAclEdge $Path
            if ($null -eq $sd) {
                return @{ ok = $null; ownerSid = $null; rules = @()
                          error = 'the ACL edge returned no descriptor' }
            }
            $ownerSid = $null
            try {
                # §101.2: PowerShell's Get-Acl Owner extension property is a
                # STRING - .Owner.Translate() would fail on every real
                # descriptor. ObjectSecurity.GetOwner([SecurityIdentifier])
                # is the REAL typed interface (SID form, no name parsing).
                $ownerSid = [string]$sd.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
            } catch {
                return @{ ok = $null; ownerSid = $null; rules = @()
                          error = ('owner SID retrieval failed: ' + $_.Exception.Message) }
            }
            $rules = @()
            foreach ($r in @($sd.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
                $rights = [uint64]([int64]$r.FileSystemRights -band 0xFFFFFFFF)
                $rules += @{
                    identitySid = [string]$r.IdentityReference.Value
                    allow = ($r.AccessControlType.ToString() -eq 'Allow')
                    rights = $rights
                }
            }
            return @{ ok = $true; ownerSid = $ownerSid; rules = $rules; error = $null }
        } catch {
            return @{ ok = $null; ownerSid = $null; rules = @(); error = $_.Exception.Message }
        }
    }.GetNewClosure()
}

# ---------------------------------------------------------------------------
# Ownership gate (P4): the run's bootstrap record is the ONLY proof that an
# account/task belongs to THIS run. A same-name account whose live SID does
# not equal the recorded created_sid is FOREIGN - never adopted, never
# inferred from a name.
# ---------------------------------------------------------------------------

function Get-D4BootstrapRecordField {
    param([object]$State, [string]$Field)
    if ($null -eq $State) { return $null }
    if ($State -is [System.Management.Automation.PSCustomObject]) {
        if ($null -eq $State.PSObject.Properties[$Field]) { return $null }
        return [string]$State.$Field
    }
    if (-not $State.ContainsKey($Field)) { return $null }
    return [string]$State[$Field]
}

function Test-D4AccountOwnershipMatch {
    param([object]$BootstrapState, [string]$LiveSid)
    $sid = Get-D4BootstrapRecordField -State $BootstrapState -Field 'created_sid'
    if (-not $sid) { return $false }
    return ($sid -eq $LiveSid)
}

function Test-D4CleanupAccountDecision {
    # The designed "disable or keep" outcome for experiment-end cleanup
    # (§93.3.6): the SAME-SID proof must hold before ANY account action is
    # offered. Returns @{ Decision='keep'|'disable'|'refuse'; Reason }.
    # Wired-in this round for TESTS only; the real disable stays in the
    # future authorized scope (§95.6).
    param([object]$BootstrapState, [string]$LiveSid, [switch]$DisableWhenOwned)
    if ($null -eq $BootstrapState -or -not (Test-D4AccountOwnershipMatch $BootstrapState $LiveSid)) {
        return @{ Decision = 'refuse'; Reason = 'same-SID proof missing - never infer ownership from a same-name account' }
    }
    if ((Get-D4BootstrapRecordField -State $BootstrapState -Field 'status') -eq 'task-registered') {
        if ($DisableWhenOwned) { return @{ Decision = 'disable'; Reason = 'run account verified by SID; task registered' } }
        return @{ Decision = 'keep'; Reason = 'run account verified by SID; disable deferred to the authorized scope' }
    }
    return @{ Decision = 'keep'; Reason = ('record status ' + (Get-D4BootstrapRecordField -State $BootstrapState -Field 'status') + '; no disable offered') }
}

# ---------------------------------------------------------------------------
# REAL OS runner (the ONLY OS calls in this module). Constructed by the entry
# in real mode; NEVER constructed or invoked by the offline suite - tests
# inject a FAKE runner and assert zero real API calls.
# Interfaces verified against the REAL cmdlet metadata (§95.2):
#   Register-ScheduledTask [Xml] set: -Xml -TaskName -TaskPath -User -Password
#   Disable-LocalUser: -Name (Set-LocalUser has NO -Enabled - never used here)
# ---------------------------------------------------------------------------

function New-D4AccountRunner {
    # Returns a hashtable of scriptblocks, each a THIN OS boundary:
    #   QueryAccount      name, ctx   -> @{ present; sid; enabled; is_admin; error }
    #   CreateAccount     name, SecureString, ctx -> @{ ok; sid; error } (New-LocalUser)
    #   DisableAccount    name, ctx   -> @{ ok; error } (Disable-LocalUser)
    #   RegisterShareTask taskName, taskPath, xml, user, plainPw, ctx -> @{ ok; error }
    #                     (Register-ScheduledTask -Xml set; in-process, no argv/env/file)
    #   QueryTask         name, ctx   -> @{ present; xml; error } (Get/Export-ScheduledTask)
    # The password reaches ONLY CreateAccount (SecureString) and
    # RegisterShareTask (a transient in-process plain string) - never
    # argv/env/log/file/Git/chat. Module availability is checked at CALL
    # time; dot-sourcing this factory has zero side effects.
    $queryAccount = {
        param([string]$Name, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                      error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            # §107.2/§109.2: ENUMERATE the account collection, then filter
            # by FULL name equality IGNORING case - Windows account names
            # are case-insensitive, so a case variant (UM_SHARE_D4 vs
            # um_share_d4) must be a collision that continues to the SID
            # ownership check, NEVER absence. Full equality only: no
            # prefix/substring/wildcard. Any enumeration failure (whatever
            # its message says) stays unconfirmed; no message keywords.
            $all = @(Get-LocalUser -ErrorAction Stop)
            $matches = @($all | Where-Object { [string]::Equals($_.Name, $Name, [System.StringComparison]::OrdinalIgnoreCase) })
            if ($matches.Count -eq 0) {
                return @{ present = $false; sid = $null; enabled = $null; is_admin = $null; error = $null }
            }
            if ($matches.Count -ne 1) {
                return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                          error = ('duplicate/multiple account matches for ' + $Name) }
            }
            $user = $matches[0]
            $isAdmin = $false
            try {
                # The admin-membership probe locates the group by its STABLE
                # SID (S-1-5-32-544) - never a localized/renameable display
                # name; a query failure leaves is_admin indeterminate and the
                # caller refuses (§95.5/§97.5).
                $adminList = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
                    Where-Object { $_.SID.Value -eq $user.SID.Value })
                $isAdmin = ($adminList.Count -gt 0)
            } catch { $isAdmin = $null }
            return @{ present = $true; sid = [string]$user.SID.Value
                      enabled = [bool]$user.Enabled; is_admin = $isAdmin; error = $null }
        } catch {
            # §107.2: an enumeration failure is NEVER absence - any query
            # error (dependency/provider/localized) stays unconfirmed
            return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                      error = ('account enumeration failed: ' + $_.Exception.Message) }
        }
    }.GetNewClosure()

    $createAccount = {
        param([string]$Name, [System.Security.SecureString]$Password, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ ok = $false; sid = $null; error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            # Created as a member of the default Users group ONLY - never
            # added to Administrators or any privileged group.
            New-LocalUser -Name $Name -Password $Password -AccountNeverExpires `
                -PasswordNeverExpires -ErrorAction Stop | Out-Null
            $user = Get-LocalUser -Name $Name -ErrorAction Stop
            return @{ ok = $true; sid = [string]$user.SID.Value; error = $null }
        } catch {
            return @{ ok = $false; sid = $null; error = $_.Exception.Message }
        }
    }.GetNewClosure()

    $disableAccount = {
        # §97.5: the disable is addressed BY SID - the record SID is proven
        # against the live account BEFORE any disable, the action itself runs
        # with -SID (never a name), and the post-verify reads the SAME SID
        # back as disabled. A same-name different-SID account receives ZERO
        # disable. (The real execution stays in the future authorized scope.)
        param([string]$Sid, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ ok = $false; error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            # Pre-prove: the SID must address a live account (the fixed
            # experiment identity), then disable BY SID - never by name.
            $pre = Get-LocalUser -SID $Sid -ErrorAction Stop
            if ($null -eq $pre -or [string]$pre.SID.Value -ne [string]$Sid) {
                return @{ ok = $false; error = ('disable pre-prove failed: SID ' + [string]$Sid + ' does not address a live account') }
            }
            # §97.5: pre-prove by SID, disable BY SID, post-verify by SID. The
            # disable call's output is CAPTURED - the real cmdlet emits the
            # disabled user object, and an uncaptured emit would corrupt the
            # boundary's return value (an array instead of the result record).
            $null = Disable-LocalUser -SID $Sid -ErrorAction Stop
            $user = Get-LocalUser -SID $Sid -ErrorAction Stop
            if ([string]$user.SID.Value -ne [string]$Sid) {
                return @{ ok = $false; error = ('disabled account SID mismatch: live ' + [string]$user.SID.Value + ', expected ' + [string]$Sid) }
            }
            if ($user.Enabled) {
                return @{ ok = $false; error = 'account still enabled after Disable-LocalUser' }
            }
            return @{ ok = $true; error = $null }
        } catch {
            return @{ ok = $false; error = $_.Exception.Message }
        }
    }.GetNewClosure()

    $registerShareTask = {
        param([string]$TaskName, [string]$TaskPath, [string]$DefinitionXml,
              [string]$UserName, [string]$PlainPassword, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'ScheduledTasks')) {
            return @{ ok = $false; error = 'ScheduledTasks module unavailable' }
        }
        try {
            # The REAL Xml parameter set of Register-ScheduledTask consumes
            # the definition document directly with -User/-Password
            # (New-ScheduledTask has NO -Xml parameter - never constructed
            # here). In-process: the credential travels only as cmdlet
            # parameters. -Force is intentionally absent: an existing
            # unmatched task must refuse (no overwrite, no takeover).
            Register-ScheduledTask -Xml $DefinitionXml -TaskName $TaskName `
                -TaskPath $TaskPath -User $UserName -Password $PlainPassword -ErrorAction Stop | Out-Null
            return @{ ok = $true; error = $null }
        } catch {
            return @{ ok = $false; error = $_.Exception.Message }
        }
    }.GetNewClosure()

    $queryTask = {
        param([string]$TaskName, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'ScheduledTasks')) {
            return @{ present = $null; xml = $null; error = 'ScheduledTasks module unavailable' }
        }
        # §107.2/§109.2: query and export are SEPARATE verdicts.
        # QUERY phase: ENUMERATE the root folder's tasks, then filter by
        # FULL name equality IGNORING case - task names are case-
        # insensitive on Windows, so a case variant is an EXISTING
        # candidate (refused by the caller's definition checks), never
        # absence; full equality only. Any enumeration failure stays
        # unconfirmed (a localized error mentioning the target never
        # means absent).
        try {
            $all = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop)
        } catch {
            return @{ present = $null; xml = $null; error = ('task enumeration failed: ' + $_.Exception.Message) }
        }
        $matches = @($all | Where-Object { [string]::Equals($_.TaskName, $TaskName, [System.StringComparison]::OrdinalIgnoreCase) })
        if ($matches.Count -eq 0) {
            return @{ present = $false; xml = $null; error = $null }
        }
        if ($matches.Count -ne 1) {
            return @{ present = $null; xml = $null; error = ('duplicate/multiple task matches for ' + $TaskName) }
        }
        # EXPORT phase: the target EXISTS - an export failure is 'cannot
        # verify an existing object', NEVER absence
        try {
            $xml = $matches[0] | Export-ScheduledTask -ErrorAction Stop
            return @{ present = $true; xml = [string]$xml; error = $null }
        } catch {
            return @{ present = $null; xml = $null
                      error = ('export failed for an EXISTING task: ' + $_.Exception.Message) }
        }
    }.GetNewClosure()

    $queryAccountBySid = {
        # §113: by-SID read-back for the recovery flow - ENUMERATE the account
        # collection, then exact SID match (OrdinalIgnoreCase; SID strings are
        # canonical) - a query failure is unconfirmed, never absence (same
        # enumeration contract as QueryAccount/§107.2).
        param([string]$Sid, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                      error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            $all = @(Get-LocalUser -ErrorAction Stop)
            $matches = @($all | Where-Object { [string]::Equals([string]$_.SID.Value, $Sid, [System.StringComparison]::OrdinalIgnoreCase) })
            if ($matches.Count -eq 0) {
                return @{ present = $false; sid = $null; enabled = $null; is_admin = $null; error = $null }
            }
            if ($matches.Count -ne 1) {
                return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                          error = ('duplicate/multiple SID matches for ' + $Sid) }
            }
            $user = $matches[0]
            $isAdmin = $false
            try {
                $adminList = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
                    Where-Object { [string]::Equals([string]$_.SID.Value, $Sid, [System.StringComparison]::OrdinalIgnoreCase) })
                $isAdmin = ($adminList.Count -gt 0)
            } catch { $isAdmin = $null }
            return @{ present = $true; sid = [string]$user.SID.Value
                      enabled = [bool]$user.Enabled; is_admin = $isAdmin; error = $null }
        } catch {
            return @{ present = $null; sid = $null; enabled = $null; is_admin = $null
                      error = ('account SID enumeration failed: ' + $_.Exception.Message) }
        }
    }.GetNewClosure()

    $resetPasswordBySid = {
        # §113: the recovery reset is addressed BY SID only (never a name) -
        # the fixed record SID is pre-proven before this call.
        param([string]$Sid, [System.Security.SecureString]$Password, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ ok = $false; error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            $null = Set-LocalUser -SID $Sid -Password $Password -ErrorAction Stop
            return @{ ok = $true; error = $null }
        } catch {
            return @{ ok = $false; error = $_.Exception.Message }
        }
    }.GetNewClosure()

    $enableAccountBySid = {
        # §113: enable is BY SID (never a name); the flow reads the facts back
        # via QueryAccountBySid.
        param([string]$Sid, [hashtable]$Ctx)
        if (-not (Get-Module -ListAvailable -Name 'Microsoft.PowerShell.LocalAccounts')) {
            return @{ ok = $false; error = 'Microsoft.PowerShell.LocalAccounts module unavailable (64-bit PowerShell required)' }
        }
        try {
            $null = Enable-LocalUser -SID $Sid -ErrorAction Stop
            return @{ ok = $true; error = $null }
        } catch {
            return @{ ok = $false; error = $_.Exception.Message }
        }
    }.GetNewClosure()

    return @{
        QueryAccount            = $queryAccount
        QueryAccountBySid       = $queryAccountBySid
        CreateAccount           = $createAccount
        DisableAccount          = $disableAccount
        RegisterShareTask       = $registerShareTask
        QueryTask               = $queryTask
        ResetAccountPasswordBySid = $resetPasswordBySid
        EnableAccountBySid      = $enableAccountBySid
    }
}

# ---------------------------------------------------------------------------
# Bootstrap plan (§93 T0 input; the experiment config does NOT exist before
# the account's SID is known). Fixed structure; NO secret fields; rule GUIDs
# are a FIXED per-run input so a recovery re-emits the SAME config (§95.4).
# ---------------------------------------------------------------------------

function Read-D4BootstrapPlan {
    param([string]$Path, [hashtable]$FileBoundary, [string]$AllowedAccount)
    if (-not (Invoke-D4File -Boundary $FileBoundary -Operation 'IsLeaf' -Arguments @($Path))) {
        throw ("d4-account-bootstrap: bootstrap plan not found: " + $Path)
    }
    $raw = Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($Path)
    $json = $null
    try { $json = $raw | ConvertFrom-Json } catch {
        throw ("d4-account-bootstrap: bootstrap plan is not valid JSON: " + $_.Exception.Message)
    }
    $required = @('run_id', 'account_name', 'python_home',
                  'runtime_source_dir', 'runtime_files', 'rule_ids')
    foreach ($k in $required) {
        if ($null -eq $json.PSObject.Properties[$k]) {
            throw ("d4-account-bootstrap: bootstrap plan missing required slot: " + $k)
        }
    }
    [void](Test-D4RunIdFormat ([string]$json.run_id))
    $account = [string]$json.account_name
    if (-not $account -or $account -notmatch '^[0-9A-Za-z_]{3,20}$') {
        throw ("d4-account-bootstrap: bootstrap plan account_name must be a bounded local name (3..20 [0-9A-Za-z_]): " + $account)
    }
    if ($AllowedAccount -and $account -ne $AllowedAccount) {
        # §95.3: the account scope is the FIXED experiment name (or an
        # explicit whitelist) - arbitrary plan input never widens it.
        throw ("d4-account-bootstrap: bootstrap plan account_name is outside the experiment account whitelist: " + $account)
    }
    if (-not [string]$json.python_home -or -not [string]$json.runtime_source_dir) {
        throw 'd4-account-bootstrap: bootstrap plan python_home and runtime_source_dir are required'
    }
    $runtimeFiles = @()
    foreach ($rf in @($json.runtime_files)) {
        if (-not $rf.file -or -not $rf.sha256 -or $rf.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw ("d4-account-bootstrap: bootstrap plan runtime file entry needs file+sha256(64hex): " + $rf)
        }
        $runtimeFiles += @{ file = [string]$rf.file; sha256 = [string]$rf.sha256 }
    }
    if ($runtimeFiles.Count -eq 0) {
        throw 'd4-account-bootstrap: bootstrap plan runtime_files must be non-empty'
    }
    $ruleIds = @()
    foreach ($rid in @($json.rule_ids)) {
        $ridText = [string]$rid
        try { [void][Guid]::Parse($ridText) } catch {
            throw ("d4-account-bootstrap: bootstrap plan rule id must be a GUID: " + $ridText)
        }
        $ruleIds += $ridText
    }
    if ($ruleIds.Count -ne 3) {
        throw ("d4-account-bootstrap: bootstrap plan rule_ids must be EXACTLY the three isolation GUIDs, got " + $ruleIds.Count)
    }
    $seen = @{}
    foreach ($ridText in $ruleIds) {
        $low = $ridText.ToLowerInvariant()
        if ($seen.ContainsKey($low)) {
            throw ("d4-account-bootstrap: bootstrap plan rule_ids carries a duplicate id: " + $ridText)
        }
        $seen[$low] = $true
    }
    # §116.2: an OPTIONAL recovery section carries the frozen old-source
    # baseline (only for -Stage T0R plans; never required elsewhere). The
    # shape is validated HERE so a malformed recovery plan cannot slip into
    # the dispatch.
    $recSec = $null
    $recProp = $json.PSObject.Properties['recovery']
    if ($null -ne $recProp -and $null -ne $recProp.Value) {
        $recRaw = $recProp.Value
        $rsOld = ''; $rsFix = ''; $rsHash = ''
        if ($recRaw -is [System.Collections.IDictionary]) {
            $rsOld = [string]$recRaw['old_run_id']; $rsFix = [string]$recRaw['fixed_sid']; $rsHash = [string]$recRaw['created_state_sha256']
        } else {
            $p1 = $recRaw.PSObject.Properties['old_run_id']; if ($p1) { $rsOld = [string]$p1.Value }
            $p2 = $recRaw.PSObject.Properties['fixed_sid']; if ($p2) { $rsFix = [string]$p2.Value }
            $p3 = $recRaw.PSObject.Properties['created_state_sha256']; if ($p3) { $rsHash = [string]$p3.Value }
        }
        if ($rsOld -notmatch '^[0-9A-Za-z-]{8,64}$' -or
            $rsFix -notmatch '^S-1-5-\d+(-\d+)*$' -or
            $rsHash -notmatch '^[0-9a-fA-F]{64}$') {
            throw "d4-account-bootstrap: the bootstrap plan recovery section is malformed (old_run_id / fixed_sid / created_state_sha256)"
        }
        [void](Test-D4RunIdFormat $rsOld)
        $recSec = @{ old_run_id = $rsOld; fixed_sid = $rsFix; created_state_sha256 = $rsHash }
    }
    return @{
        run_id = [string]$json.run_id
        account_name = $account
        python_home = [string]$json.python_home
        runtime_source_dir = [string]$json.runtime_source_dir
        runtime_files = $runtimeFiles
        rule_ids = $ruleIds
        recovery = $recSec
    }
}

# ---------------------------------------------------------------------------
# Orchestration: Invoke-D4AccountBootstrap. ONE call = the §93.3.2 "one
# elevated deployment process": store-ready -> precheck -> intent -> create
# -> SID -> privilege recheck -> register -> post-register export verify ->
# record. The credential is a LOCAL SecureString only, generated ONCE at the
# creation moment, held in process for create+register, and NEVER regenerated
# or persisted. A fresh create+register can therefore only happen in a single
# invocation from 'absent'; any later entry that still needs the credential
# fails closed as 'credential-unavailable'. Every status write goes through
# the live transition table; runner THROWS are converted to structured
# unconfirmed outcomes; the temp password references are dropped in finally.
# ---------------------------------------------------------------------------

function Invoke-D4AccountBootstrap {
    param(
        [hashtable]$Contract,
        [string]$RunId,
        [string]$AccountName,
        [string]$ShareTaskName,
        [scriptblock]$DefinitionBuilder,
        [string]$PackageHash,
        [string]$StatePath,
        [hashtable]$AccountRunner,
        [hashtable]$FileBoundary,
        [scriptblock]$StoreGuard,
        [scriptblock]$StoreProbe,
        [scriptblock]$AclProbe,
        [string]$PlanIdentity
    )
    if (-not $AccountRunner -or -not $AccountRunner.ContainsKey('QueryAccount')) {
        throw 'd4-account-bootstrap: an account runner (real or injected) is required; failing closed with zero OS calls'
    }
    if (-not $DefinitionBuilder) {
        throw 'd4-account-bootstrap: a share-task definition builder is required (built with the CREATED SID; never a pre-SID guess)'
    }
    if (-not $PackageHash) {
        throw 'd4-account-bootstrap: the package hash binding is required (records never cross packages)'
    }
    if (-not $PlanIdentity) {
        throw 'd4-account-bootstrap: the plan identity binding is required (records never cross run plans)'
    }
    if (-not (Get-Command 'Test-D4TaskDefinitionShape' -ErrorAction SilentlyContinue)) {
        throw 'd4-account-bootstrap: the shared task-shape verifier (execute-d4-experiment-lib) must be loaded first'
    }
    $taskCommand = Join-Path $Contract['ExperimentRoot'] 'run-live.cmd'
    # §97.2: TaskPath is the task FOLDER (the root folder is '\'), NOT the
    # task name - registration and query must address the SAME object via
    # folder '\' + the separate TaskName (Register-ScheduledTask semantics).
    $taskPathRooted = '\'

    # ---- §95.3/§97.3: protected store + bindings BEFORE any account action ----
    $store = Test-D4BootstrapStoreReady -StatePath $StatePath `
        -BootstrapRoot $Contract['BootstrapRoot'] -RunId $RunId -AccountName $AccountName `
        -TaskName $ShareTaskName -PackageHash $PackageHash -PlanIdentity $PlanIdentity `
        -FileBoundary $FileBoundary -StoreGuard $StoreGuard -StoreProbe $StoreProbe `
        -AclProbe $AclProbe
    if (-not $store.ok) {
        return @{ Ok = $false; Refused = 'store-not-protected'; Reason = $store.reason
                  Status = 'refused-store'; CreatedSid = $null; config_json = $null }
    }

    $record = Read-D4BootstrapState -StatePath $StatePath -RunId $RunId -AccountName $AccountName `
        -TaskName $ShareTaskName -PackageHash $PackageHash -PlanIdentity $PlanIdentity `
        -FileBoundary $FileBoundary
    if ($null -eq $record) {
        $record = New-D4BootstrapState -RunId $RunId -AccountName $AccountName `
            -TaskName $ShareTaskName -PackageHash $PackageHash -PlanIdentity $PlanIdentity
    }
    $state = @{}
    if ($record -is [System.Collections.IDictionary]) {
        foreach ($k in @($record.Keys)) { $state[$k] = $record[$k] }
    } else {
        foreach ($p in $record.PSObject.Properties) { $state[$p.Name] = $p.Value }
    }

    # ---- outcome helper (every failure lands here; unconfirmed from ANY state) ----
    function Write-UnconfirmedState([hashtable]$S, [string]$Reason, [hashtable]$Boundary,
            [string]$Path, [string]$Sid, [string]$RefusedTag = 'unconfirmed') {
        $S['status'] = 'unconfirmed'
        $S['unconfirmed_reason'] = $Reason
        try {
            [void](Save-D4BootstrapState -StatePath $Path -State $S -FileBoundary $Boundary)
        } catch {
            # The failure record itself could not be persisted - the returned
            # verdict stays unconfirmed and the throw is NOT swallowed.
            $S['unconfirmed_reason'] = ($Reason + ' (record write failed: ' + $_.Exception.Message + ')')
        }
        return @{ Ok = $false; Refused = $RefusedTag; Reason = $S['unconfirmed_reason']
                  Status = 'unconfirmed'; CreatedSid = $Sid; config_json = $null }
    }
    # ---- live transition guard (Test-D4BootstrapTransition is consulted on
    # EVERY status change; an illegal step refuses without a fake rollback) ----
    function Step-Status([hashtable]$S, [string]$To) {
        if (Test-D4BootstrapTransition ([string]$S['status']) $To) {
            $S['status'] = $To
            return $true
        }
        return $false
    }
    # ---- account usability: enabled + explicitly NOT admin + SID present ----
    function Test-D4AccountUsable([hashtable]$Facts) {
        if ($Facts.present -ne $true) { return @{ ok = $false; reason = 'account not present' } }
        if ($Facts.enabled -ne $true) { return @{ ok = $false; reason = 'account disabled or enabled state unconfirmed' } }
        if ($Facts.is_admin -ne $false) { return @{ ok = $false; reason = ('account admin membership is TRUE or indeterminate: ' + [string]$Facts.is_admin) } }
        if (-not [string]$Facts.sid) { return @{ ok = $false; reason = 'live SID missing' } }
        return @{ ok = $true; reason = '' }
    }

    try {
        $live = & $AccountRunner['QueryAccount'] $AccountName @{}
    } catch {
        return (Write-UnconfirmedState $state ('account query threw: ' + $_.Exception.Message) $FileBoundary $StatePath $null)
    }

    # ---- ground truth: the account ----
    if ($live.present -eq $null) {
        return (Write-UnconfirmedState $state ('account query unconfirmed: ' + [string]$live.error) $FileBoundary $StatePath $null)
    }

    $recordedSid = Get-D4BootstrapRecordField -State $state -Field 'created_sid'
    if ($live.present -eq $true) {
        if (-not $recordedSid -or $live.sid -ne $recordedSid) {
            $reason = if (-not $recordedSid) { 'account ' + $AccountName + ' exists (SID ' + $live.sid + ') with no created_sid in the run record; ownership cannot be proven' }
                      else { 'account ' + $AccountName + ' SID changed from the recorded ' + $recordedSid + ' to ' + $live.sid + '; ownership cannot be proven' }
            return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $null 'foreign-or-unprovable')
        }
        # Owned account present: privilege facts are re-read and MUST be
        # usable (enabled, explicitly non-admin) before ANY reuse (§95.5).
        $usable = Test-D4AccountUsable $live
        if (-not $usable.ok) {
            return (Write-UnconfirmedState $state ('owned account is not usable: ' + $usable.reason) $FileBoundary $StatePath $recordedSid)
        }
        if ($state['status'] -eq 'task-registered') {
            # Idempotent continuation: verify the FULL task shape via the
            # shared reviewed verifier; no credential is needed.
            $taskNow = & $AccountRunner['QueryTask'] $ShareTaskName @{}
            if ($taskNow.present -ne $true) {
                $reason = 'record says task-registered but the share task is ' + $(if ($taskNow.present -eq $null) { 'unconfirmed: ' + $taskNow.error } else { 'absent' }) + '; inconsistent -> stop'
                return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $recordedSid)
            }
            $taskSpec = @{ Name = $ShareTaskName; IdentitySid = $recordedSid; LogonType = 'Password';
                           Command = $taskCommand }
            # §97.2: the logon context lives IN THE SPEC (the single source of
            # truth) - the shared verifier refuses a spec without one; the
            # explicit parameter below cross-checks it.
            $shape = Test-D4TaskDefinitionShape -DefinitionXml ([string]$taskNow.xml) `
                -Task $taskSpec -Trigger 'ONDEMAND' -LogonType ([string]$taskSpec['LogonType'])
            if ($shape.State -ne 'owned') {
                $reason = 'task-registered record but the live task fails the FULL definition shape (' + $shape.State + ': ' + $shape.Reason + '); refusing re-registration'
                return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $recordedSid)
            }
            return @{ Ok = $true; Refused = $null
                      Reason = 'idempotent: account owned (same SID, usable) and task already registered matching the FULL shape; no credential needed'
                      Status = 'task-registered'; CreatedSid = $recordedSid; config_json = $null }
        }
        $reason = 'account ' + $AccountName + ' is already created (owned, SID ' + $recordedSid + ') but the share task is not registered and the ephemeral credential is unavailable (never persisted); authorized disable/keep required'
        return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $recordedSid)
    }

    # ---- account absent ----
    if ($state['status'] -notin 'absent', 'creation-intent') {
        if (-not $recordedSid) {
            $reason = ('run record is ' + [string]$state['status'] +
                       ' with no created_sid but the account is absent; inconsistent -> stop')
            return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $null)
        }
        $reason = ('account recorded as created (SID ' + $recordedSid +
                   ') no longer exists; out-of-band deletion suspected -> stop, never re-create')
        return (Write-UnconfirmedState $state $reason $FileBoundary $StatePath $recordedSid)
    }

    # ---- fresh create + register, ONE process, ONE password ----
    if (-not (Step-Status $state 'creation-intent')) {
        return (Write-UnconfirmedState $state ('illegal bootstrap transition ' + [string]$state['status'] + ' -> creation-intent (no fake rollback)') $FileBoundary $StatePath $null)
    }
    $state['intent_at'] = (Get-Date).ToUniversalTime().ToString('o')
    [void](Save-D4BootstrapState -StatePath $StatePath -State $state -FileBoundary $FileBoundary)

    # ONE credential lifetime: the password is generated here and dropped in
    # the single finally below - every early exit passes through it (no claim
    # of absolute memory erasure, only lifecycle containment).
    $password = New-D4StrongPassword
    $plainTmp = $null
    try {
        try {
            $create = & $AccountRunner['CreateAccount'] $AccountName $password @{}
        } catch {
            return (Write-UnconfirmedState $state ('account creation threw: ' + $_.Exception.Message) $FileBoundary $StatePath $null)
        }
        if (-not $create.ok -or -not $create.sid) {
            return (Write-UnconfirmedState $state ('account creation failed or SID unknown: ' + [string]$create.error) $FileBoundary $StatePath $null)
        }
        # §97.5: the SID the creation API RETURNED is persisted FIRST as a
        # creation fact - any later verification failure keeps that fact on
        # disk (unconfirmed + the SID), never dropped, never re-inferred from
        # a same-name account.
        if (-not (Step-Status $state 'created')) {
            return (Write-UnconfirmedState $state ('illegal bootstrap transition ' + [string]$state['status'] + ' -> created (no fake rollback)') $FileBoundary $StatePath ([string]$create.sid))
        }
        $state['created_sid'] = [string]$create.sid
        $state['created_at'] = (Get-Date).ToUniversalTime().ToString('o')
        [void](Save-D4BootstrapState -StatePath $StatePath -State $state -FileBoundary $FileBoundary)
        # First-creation privilege verification: the facts are READ back, never
        # assumed from the New-LocalUser call (§95.5).
        try {
            $liveAfter = & $AccountRunner['QueryAccount'] $AccountName @{}
        } catch {
            return (Write-UnconfirmedState $state ('post-create account query threw: ' + $_.Exception.Message) $FileBoundary $StatePath ([string]$create.sid))
        }
        $usableAfter = Test-D4AccountUsable $liveAfter
        if (-not $usableAfter.ok -or [string]$liveAfter.sid -ne [string]$create.sid) {
            return (Write-UnconfirmedState $state ('created account fails the usable/SID check: ' + $usableAfter.reason) $FileBoundary $StatePath ([string]$create.sid))
        }

        try {
            $taskBefore = & $AccountRunner['QueryTask'] $ShareTaskName @{}
        } catch {
            return (Write-UnconfirmedState $state ('task query threw: ' + $_.Exception.Message) $FileBoundary $StatePath ([string]$create.sid))
        }
        if ($taskBefore.present -eq $null) {
            return (Write-UnconfirmedState $state ('task query unconfirmed: ' + [string]$taskBefore.error) $FileBoundary $StatePath ([string]$create.sid))
        }
        if ($taskBefore.present -eq $true) {
            # A same-name task existed at registration time without a matching
            # record - do NOT overwrite it.
            return (Write-UnconfirmedState $state 'a same-name share task already exists before this run''s registration; refusing overwrite (no takeover)' $FileBoundary $StatePath ([string]$create.sid))
        }
        # The definition is built NOW, bound to the CREATED SID (never a
        # pre-creation guess); the credential stays a local in-process value
        # for the single registration call.
        try {
            $definitionXml = [string](& $DefinitionBuilder ([string]$create.sid))
        } catch {
            return (Write-UnconfirmedState $state ('share task definition build threw: ' + $_.Exception.Message) $FileBoundary $StatePath ([string]$create.sid))
        }
        if (-not $definitionXml) {
            return (Write-UnconfirmedState $state 'share task definition could not be built with the created SID' $FileBoundary $StatePath ([string]$create.sid))
        }
        try {
            # R147: PS 5.1 parses `New-Object SomeType(a, $b).Member` so that `.Member`
            # is swallowed and the expression yields the OBJECT (its [string] is the
            # literal type name) - the registration previously received
            # 'System.Net.NetworkCredential' instead of the password, so Task
            # Scheduler validated a WRONG password (4625 SubStatus 0xC000006A).
            # The explicit parens apply .Password to the constructed instance.
            $plainTmp = (New-Object System.Net.NetworkCredential('', $password)).Password
        } catch {
            return (Write-UnconfirmedState $state ('the ephemeral credential could not be materialized for the registration call: ' + $_.Exception.Message) $FileBoundary $StatePath ([string]$create.sid))
        }
        try {
            $reg = & $AccountRunner['RegisterShareTask'] $ShareTaskName $taskPathRooted $definitionXml $AccountName $plainTmp @{}
        } catch {
            $reg = @{ ok = $false; error = ('registration call threw: ' + $_.Exception.Message) }
        }
        if (-not $reg.ok) {
            $state['task_register_result'] = 'failed'
            return (Write-UnconfirmedState $state ('task registration failed; the ephemeral credential is dropped (no regeneration): ' + [string]$reg.error) $FileBoundary $StatePath ([string]$create.sid))
        }
        # Post-register EXPORT verification: the returned ok never substitutes
        # for reading the registered definition back (§95.4).
        try {
            $taskAfter = & $AccountRunner['QueryTask'] $ShareTaskName @{}
        } catch {
            return (Write-UnconfirmedState $state ('post-register task query threw: ' + $_.Exception.Message) $FileBoundary $StatePath ([string]$create.sid))
        }
        if ($taskAfter.present -ne $true) {
            return (Write-UnconfirmedState $state ('post-register export verification failed: task ' + $(if ($taskAfter.present -eq $null) { 'unconfirmed: ' + $taskAfter.error } else { 'absent' })) $FileBoundary $StatePath ([string]$create.sid))
        }
        $taskSpecAfter = @{ Name = $ShareTaskName; IdentitySid = [string]$create.sid; LogonType = 'Password';
                            Command = $taskCommand }
        # §97.2: the post-register read-back verifies the logon context
        # EXPLICITLY (spec LogonType travels as the expected value).
        $shapeAfter = Test-D4TaskDefinitionShape -DefinitionXml ([string]$taskAfter.xml) `
            -Task $taskSpecAfter -Trigger 'ONDEMAND' -LogonType ([string]$taskSpecAfter['LogonType'])
        if ($shapeAfter.State -ne 'owned') {
            return (Write-UnconfirmedState $state ('post-register export verification failed the FULL shape (' + $shapeAfter.State + ': ' + $shapeAfter.Reason + ')') $FileBoundary $StatePath ([string]$create.sid))
        }
    } finally {
        # Bound the transient credential lifetime: the plain form and the
        # SecureString reference are dropped on EVERY exit path.
        $plainTmp = $null
        $password = $null
    }
    if (-not (Step-Status $state 'task-registered')) {
        return (Write-UnconfirmedState $state ('illegal bootstrap transition ' + [string]$state['status'] + ' -> task-registered (no fake rollback)') $FileBoundary $StatePath ([string]$create.sid))
    }
    $state['task_registered_at'] = (Get-Date).ToUniversalTime().ToString('o')
    $state['task_register_result'] = 'registered'
    [void](Save-D4BootstrapState -StatePath $StatePath -State $state -FileBoundary $FileBoundary)

    return @{
        Ok = $true; Refused = $null
        Reason = 'account created and share task registered with an ephemeral credential (password not persisted); exported definition verified'
        Status = 'task-registered'; CreatedSid = [string]$create.sid; config_json = $null
    }
}

function Invoke-D4AccountRecovery {
    # §113: AUTHORIZED same-SID recovery entry - runs ONLY when the caller
    # explicitly selects recovery (entry -Stage T0R). Never creates an
    # account and NEVER loosens Invoke-D4AccountBootstrap's foreign-account
    # refusal. Preconditions (all read-only, all before ANY side effect):
    # the trusted protected store, the OLD creation record (original path +
    # byte hash + created_sid == the fixed SID), the live account (same SID,
    # non-admin, DISABLED), and zero experiment occupancy. Then, in ONE
    # elevated process: persist recovery-intent -> reset the password BY SID
    # -> enable BY SID (minimal window) -> register the share task with the
    # audited Xml/User/Password set (root path, no Force) -> verify the
    # exported definition. Every side effect is intent-recorded first and
    # evidence-recorded after; ANY failure goes to unconfirmed (with the SID
    # kept, so a SID-addressed disable stays executable) and NEVER
    # auto-rotates. A completed record (task-registered) is idempotently
    # verified on re-entry (no second register, no rotation).
    param(
        [hashtable]$Contract,
        [string]$RunId,
        [string]$AccountName,
        [string]$ShareTaskName,
        [string]$OldRunId,
        [string]$OldStatePath,
        [string]$FixedSid,
        [scriptblock]$DefinitionBuilder,
        [string]$PackageHash,
        [string]$PlanIdentity,
        [string]$RecoveryStatePath,
        [string]$ExpectedOldStateSha256,
        [hashtable]$AccountRunner,
        [hashtable]$FileBoundary,
        [scriptblock]$StoreProbe,
        [scriptblock]$AclProbe,
        [scriptblock]$OccupancyProbe
    )
    foreach ($op in @('QueryAccount', 'QueryAccountBySid', 'ResetAccountPasswordBySid',
                      'EnableAccountBySid', 'RegisterShareTask', 'QueryTask', 'DisableAccount')) {
        if (-not $AccountRunner -or -not $AccountRunner.ContainsKey($op)) {
            throw ('d4-account-bootstrap: recovery requires the ' + $op + ' runner op (real or injected); failing closed with zero OS calls')
        }
    }
    if (-not $DefinitionBuilder) {
        throw 'd4-account-bootstrap: recovery requires the share-task definition builder (bound to the fixed SID)'
    }
    if (-not $PackageHash -or -not $PlanIdentity) {
        throw 'd4-account-bootstrap: recovery requires the package hash and plan identity bindings (records never cross packages/plans)'
    }
    if (-not $OldRunId -or -not $OldStatePath -or -not $FixedSid) {
        throw 'd4-account-bootstrap: recovery requires the old run id, old record path and the fixed SID'
    }
    if (-not $ExpectedOldStateSha256 -or $ExpectedOldStateSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'd4-account-bootstrap: recovery requires the 64-hex frozen old-record hash (the reviewed recovery plan input, never the read being verified)'
    }
    if (-not (Get-Command 'Test-D4TaskDefinitionShape' -ErrorAction SilentlyContinue)) {
        throw 'd4-account-bootstrap: the shared task-shape verifier (execute-d4-experiment-lib) must be loaded first'
    }
    if (-not $OccupancyProbe) {
        throw 'd4-account-bootstrap: recovery requires the occupancy probe (real or injected)'
    }
    $taskCommand = Join-Path $Contract['ExperimentRoot'] 'run-live.cmd'
    $taskPathRooted = '\'

    # ---- read-only trust gate BEFORE any recovery side effect ----
    $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $Contract['BootstrapRoot'] `
        -StatePath $RecoveryStatePath -AclProbe $AclProbe -StoreProbe $StoreProbe `
        -FileBoundary $FileBoundary -CheckLeaf
    if (-not $trust.trusted) {
        return @{ Ok = $false; Refused = 'store-not-protected'; Reason = $trust.reason
                  Status = 'refused-store'; CreatedSid = $null; config_json = $null }
    }
    # §116.2: the OLD creation record leaf is a SEPARATE consumer target - it
    # must pass the SAME read-only trust gate (reparse chain + owner + write
    # ACE) before its content is adopted in any way.
    $oldTrust = Test-D4BootstrapStoreTrust -BootstrapRoot $Contract['BootstrapRoot'] `
        -StatePath $OldStatePath -AclProbe $AclProbe -StoreProbe $StoreProbe `
        -FileBoundary $FileBoundary -CheckLeaf
    if (-not $oldTrust.trusted) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('the OLD creation record leaf is NOT trusted (' + $oldTrust.reason + '); zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }

    $state = $null
    try { $state = Read-D4RecoveryState -StatePath $RecoveryStatePath -RunId $RunId `
            -AccountName $AccountName -TaskName $ShareTaskName -PackageHash $PackageHash `
            -PlanIdentity $PlanIdentity -FileBoundary $FileBoundary } catch {
        return @{ Ok = $false; Refused = 'recovery-record-unreadable'; Reason = $_.Exception.Message
                  Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
    }
    if ($null -eq $state) {
        $state = New-D4RecoveryState -RunId $RunId -AccountName $AccountName `
            -TaskName $ShareTaskName -CreatedStatePath $OldStatePath -CreatedStateSha256 '' `
            -CreatedSid $FixedSid -PackageHash $PackageHash -PlanIdentity $PlanIdentity
    }
    # PSCustomObject has NO indexer - materialize an IDictionary for the flow
    # (same as the bootstrap orchestration does; $state['field'] must work).
    $stateBox = @{}
    if ($state -is [System.Collections.IDictionary]) {
        foreach ($k in @($state.Keys)) { $stateBox[[string]$k] = $state[$k] }
    } else {
        foreach ($p in $state.PSObject.Properties) { $stateBox[$p.Name] = $p.Value }
    }
    $state = $stateBox
    if ([string]$state['status'] -ne 'absent') {
        # §116.2: an EXISTING recovery record is consumed ONLY when it binds
        # the same fixed SID, the same old source path AND the frozen old
        # source hash (not merely 'a field exists').
        if ([string]$state['created_sid'] -ne $FixedSid -or
            [string]$state['created_state_path'] -ne $OldStatePath -or
            -not [string]::Equals([string]$state['created_state_sha256'], $ExpectedOldStateSha256, [System.StringComparison]::OrdinalIgnoreCase)) {
            return @{ Ok = $false; Refused = 'recovery-bound-binding'; Reason = 'the existing recovery record does not bind this run''s fixed SID / old source path / old source hash; no consumption'
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
    }

    # ---- outcome helper (every failure lands here; unconfirmed from ANY state) ----
    function Write-RecoveryUnconfirmed([hashtable]$S, [string]$Reason, [hashtable]$Boundary,
            [string]$Path) {
        $S['status'] = 'unconfirmed'
        $S['unconfirmed_reason'] = $Reason
        try {
            [void](Save-D4BootstrapState -StatePath $Path -State $S -FileBoundary $Boundary)
        } catch {
            $S['unconfirmed_reason'] = ($Reason + ' (record write failed: ' + $_.Exception.Message + ')')
        }
        return @{ Ok = $false; Refused = 'unconfirmed'; Reason = $S['unconfirmed_reason']
                  Status = 'unconfirmed'; CreatedSid = [string]$FixedSid; config_json = $null }
    }
    function Step-RecoveryStatus([hashtable]$S, [string]$To) {
        if (Test-D4RecoveryTransition ([string]$S['status']) $To) {
            $S['status'] = $To
            return $true
        }
        return $false
    }
    # §116.4: bounded close used on EVERY failure AFTER the reset/enable was
    # attempted and on final-accounting failures - the disable is by the fixed
    # SID, post-verified, and a close failure NEVER hides the original reason.
    function Close-RecoveryFailure([hashtable]$S, [string]$Reason) {
        $cr = Invoke-D4RecoveryClose -Contract $Contract -RunId $RunId `
            -AccountName $AccountName -TaskName $ShareTaskName `
            -FixedSid $FixedSid -RecoveryStatePath $RecoveryStatePath `
            -PackageHash $PackageHash -PlanIdentity $PlanIdentity `
            -AccountRunner $AccountRunner -FileBoundary $FileBoundary `
            -StoreProbe $StoreProbe -AclProbe $AclProbe `
            -PreserveReason $Reason
        if ($cr.ok) {
            # §116.4: the close VERIFIED (Enabled=False) and persisted the
            # disabled terminal WITH the original failure reason - the failure
            # verdict returns WITHOUT overwriting that terminal state.
            $Reason += '; recovery close: disabled by the fixed SID and post-verified (Enabled=False)'
            return @{ Ok = $false; Refused = 'unconfirmed'; Reason = $Reason
                      Status = 'unconfirmed'; CreatedSid = [string]$FixedSid; config_json = $null }
        }
        $Reason += ('; recovery close FAILED (not recorded as disabled): ' + [string]$cr.reason)
        return (Write-RecoveryUnconfirmed $S $Reason $FileBoundary $RecoveryStatePath)
    }

    # ---- §113.3.2 read-only ELIGIBILITY (all before ANY side effect) ----
    # (1) the OLD creation record: original path present, byte hash recorded,
    #     run/account/task/created_sid facts bound to the recovery inputs.
    try {
        $oldHash = [string](Invoke-D4File -Boundary $FileBoundary -Operation 'Hash' -Arguments @($OldStatePath))
    } catch {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('the old creation record cannot be read: ' + $_.Exception.Message)
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if (-not $oldHash) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = 'the old creation record has no verifiable hash; zero side effects'
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    # §116.2: the current hash is compared against the FROZEN expected value
    # from the reviewed recovery plan - a drift of the old source refuses.
    if (-not [string]::Equals($oldHash, $ExpectedOldStateSha256, [System.StringComparison]::OrdinalIgnoreCase)) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('the old creation record hash does not match the frozen recovery plan (expected ' + $ExpectedOldStateSha256 + ', live ' + $oldHash + '); zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    try {
        $oldRaw = [string](Invoke-D4File -Boundary $FileBoundary -Operation 'ReadText' -Arguments @($OldStatePath))
        $oldRec = $oldRaw | ConvertFrom-Json
    } catch {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('the old creation record is unreadable/corrupt: ' + $_.Exception.Message)
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if (-not $oldRec -or [string]$oldRec.schema -ne 'd4-account-bootstrap' -or
        [string]$oldRec.run_id -ne $OldRunId -or
        [string]$oldRec.account_name -ne $AccountName -or
        [string]$oldRec.task_name -ne $ShareTaskName) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = 'the old creation record is not this batch''s record (run/account/task binding mismatch); zero side effects'
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    $oldCreatedSid = [string]$oldRec.created_sid
    if ([string]::IsNullOrEmpty($oldCreatedSid) -or $oldCreatedSid -ne $FixedSid) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = 'the old creation record created_sid does not equal the fixed recovery SID; zero side effects'
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }

    # (2) live account: present, SAME SID (exact), non-admin, CURRENTLY disabled.
    try {
        $live = & $AccountRunner['QueryAccount'] $AccountName @{}
    } catch {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('live account query threw: ' + $_.Exception.Message)
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if ($live.present -eq $null) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = 'live account unconfirmed: ' + [string]$live.error
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if ($live.present -ne $true -or [string]$live.sid -ne $FixedSid) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('the account is ' + $(if ($live.present -ne $true) { 'absent' } else { 'present with a DIFFERENT SID ' + [string]$live.sid }) + '; recovery requires the exact fixed SID - zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if ($live.is_admin -ne $false) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('recovery refuses an admin/indeterminate account (is_admin=' + [string]$live.is_admin + '); zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    # Enabled-state is flow-specific: the COMPLETED-continuation requires the
    # account ENABLED (a finished recovery leaves it usable until the closing
    # disable); the FRESH attempt requires it DISABLED (checked below, after
    # the re-entry gate).

    # (4) re-entry gate: an EXISTING recovery record that is not complete is
    # NEVER re-attempted (the reset may already have happened with the
    # password lost in-process) - keep it, disable by SID elsewhere, report.
    $currentStatus = [string]$state['status']
    if ($currentStatus -eq 'task-registered') {
        # §113.3.5 reconcile: a COMPLETED recovery leaves the account ENABLED
        # until the closing disable - a disabled account with a completed
        # record is inconsistent (the closing already ran or out-of-band).
        if ($live.enabled -ne $true) {
            return @{ Ok = $false; Refused = 'unconfirmed'; Reason = ('recovery record says task-registered but the account is currently disabled; inconsistent -> stop')
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
        if ([string]$state['created_state_sha256'] -ne $oldHash) {
            return @{ Ok = $false; Refused = 'recovery-attempted'; Reason = 'the old creation record hash changed since the recovery record was written; no exemption'
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
        # Idempotent continuation: the task must EXIST with the full shape.
        try {
            $taskNow = & $AccountRunner['QueryTask'] $ShareTaskName @{}
        } catch {
            return @{ Ok = $false; Refused = 'unconfirmed'; Reason = ('post-recovery task query threw: ' + $_.Exception.Message)
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
        if ($taskNow.present -ne $true) {
            return @{ Ok = $false; Refused = 'unconfirmed'; Reason = ('recovery record says task-registered but the task is ' + $(if ($taskNow.present -eq $null) { 'unconfirmed: ' + $taskNow.error } else { 'absent' }) + '; inconsistent -> stop')
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
        $spec = @{ Name = $ShareTaskName; IdentitySid = $FixedSid; LogonType = 'Password'; Command = $taskCommand }
        $shape = Test-D4TaskDefinitionShape -DefinitionXml ([string]$taskNow.xml) `
            -Task $spec -Trigger 'ONDEMAND' -LogonType ([string]$spec['LogonType'])
        if ($shape.State -ne 'owned') {
            return @{ Ok = $false; Refused = 'unconfirmed'; Reason = ('task-registered recovery record but the live task fails the FULL definition shape (' + $shape.State + ': ' + $shape.Reason + '); refusing to re-register')
                      Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
        }
        return @{ Ok = $true; Refused = $null
                  Reason = 'idempotent: recovery completed (task-registered) and the live task matches the FULL shape; no re-register, no rotation'
                  Status = 'task-registered'; CreatedSid = $FixedSid; config_json = $null }
    }
    if ($currentStatus -eq 'disabled') {
        # §118.2: ALREADY CLOSED - verify the terminal and return idempotently
        # (no re-rotation, no second disable storm).
        try {
            $already = & $AccountRunner['QueryAccountBySid'] $FixedSid @{}
        } catch { $already = @{ present = $null; sid = $null; enabled = $null; error = $_.Exception.Message } }
        if ($already.present -eq $true -and [string]$already.sid -eq $FixedSid -and $already.enabled -eq $false) {
            return @{ Ok = $true; Refused = $null
                      Reason = 'already closed: account disabled by the fixed SID (post-verified); no re-rotation'
                      Status = 'disabled'; CreatedSid = $FixedSid; config_json = $null }
        }
        return @{ Ok = $false; Refused = 'unconfirmed'; Reason = 'recovery record says disabled but the SAME SID is not confirmed disabled; no re-rotation'
                  Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
    }
    if ($currentStatus -ne 'absent') {
        # §118.2: a TRUSTED (binding-verified above) interrupted/attempted
        # record is closed by ONE bounded disable before refusing any new
        # attempt - zero reset/enable, no rotation; an untrusted/wrong-binding
        # record never reaches here (refused earlier with zero side effects).
        $crI = Invoke-D4RecoveryClose -Contract $Contract -RunId $RunId `
            -AccountName $AccountName -TaskName $ShareTaskName `
            -FixedSid $FixedSid -RecoveryStatePath $RecoveryStatePath `
            -PackageHash $PackageHash -PlanIdentity $PlanIdentity `
            -AccountRunner $AccountRunner -FileBoundary $FileBoundary `
            -StoreProbe $StoreProbe -AclProbe $AclProbe
        if ($crI.ok) {
            return @{ Ok = $false; Refused = 'recovery-attempted-closed'; Reason = ('recovery already attempted (status ' + $currentStatus + '); bounded close disabled the fixed SID (post-verified); NO second reset')
                      Status = 'disabled'; CreatedSid = $FixedSid; config_json = $null }
        }
        return @{ Ok = $false; Refused = 'recovery-attempted'; Reason = ('recovery already attempted (status ' + $currentStatus + ') and the bounded close FAILED (' + [string]$crI.reason + '); NO second reset, NO disabled terminal')
                  Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
    }

    # FRESH attempt: experiment occupancy must be clean (for a COMPLETED
    # record the own task is expected and re-verified by shape above - the
    # occupancy probe is NOT consulted there).
    try {
        $occ = & $OccupancyProbe
    } catch {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('occupancy probe threw: ' + $_.Exception.Message)
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }
    if ($null -eq $occ -or $occ.clean -ne $true) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('recovery occupancy is NOT clean (' + [string]$occ.detail + '); zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }

    # FRESH attempt: the account must be CURRENTLY DISABLED (the reset+enable
    # are the recovery's own minimal window; an enabled account is refused).
    if ($live.enabled -ne $false) {
        return @{ Ok = $false; Refused = 'eligibility'; Reason = ('recovery requires the account to be CURRENTLY DISABLED (enabled=' + [string]$live.enabled + '); zero side effects')
                  Status = 'refused'; CreatedSid = $null; config_json = $null }
    }

    # ---- persist recovery-intent BEFORE any side effect (crash-safe anchor) ----
    $state['created_state_sha256'] = $oldHash
    if (-not (Step-RecoveryStatus $state 'recovery-intent')) {
        return (Write-RecoveryUnconfirmed $state ('illegal recovery transition ' + $currentStatus + ' -> recovery-intent (no fake rollback)') $FileBoundary $RecoveryStatePath)
    }
    $state['intent_at'] = (Get-Date).ToUniversalTime().ToString('o')
    try {
        [void](Save-D4BootstrapState -StatePath $RecoveryStatePath -State $state -FileBoundary $FileBoundary)
    } catch {
        return @{ Ok = $false; Refused = 'unconfirmed'; Reason = ('recovery intent could not be persisted: ' + $_.Exception.Message)
                  Status = 'unconfirmed'; CreatedSid = $FixedSid; config_json = $null }
    }

    # ONE credential lifetime: generated here, dropped in the single finally
    # below (no claim of absolute memory erasure, only lifecycle containment).
    $password = New-D4StrongPassword
    $plainTmp = $null
    try {
        # reset the password BY SID (exactly once)
        try {
            $reset = & $AccountRunner['ResetAccountPasswordBySid'] $FixedSid $password @{}
        } catch {
            return (Close-RecoveryFailure $state ('password reset (by SID) threw: ' + $_.Exception.Message))
        }
        if (-not $reset.ok) {
            return (Close-RecoveryFailure $state ('password reset (by SID) failed: ' + [string]$reset.error))
        }
        if (-not (Step-RecoveryStatus $state 'password-reset')) {
            return (Close-RecoveryFailure $state ('illegal recovery transition -> password-reset (no fake rollback)'))
        }
        $state['reset_at'] = (Get-Date).ToUniversalTime().ToString('o')
        try {
            [void](Save-D4BootstrapState -StatePath $RecoveryStatePath -State $state -FileBoundary $FileBoundary)
        } catch {
            # §116.4: a reset-accounting write failure still runs the bounded close.
            return (Close-RecoveryFailure $state ('password-reset accounting write failed; bounded close: ' + $_.Exception.Message))
        }

        # enable BY SID for the minimal registration window (exactly once)
        try {
            $enable = & $AccountRunner['EnableAccountBySid'] $FixedSid @{}
        } catch {
            return (Close-RecoveryFailure $state ('enable (by SID) threw: ' + $_.Exception.Message))
        }
        if (-not $enable.ok) {
            return (Close-RecoveryFailure $state ('enable (by SID) failed: ' + [string]$enable.error))
        }
        if (-not (Step-RecoveryStatus $state 'enabled')) {
            return (Close-RecoveryFailure $state ('illegal recovery transition -> enabled (no fake rollback)'))
        }
        $state['enabled_at'] = (Get-Date).ToUniversalTime().ToString('o')
        try {
            [void](Save-D4BootstrapState -StatePath $RecoveryStatePath -State $state -FileBoundary $FileBoundary)
        } catch {
            return (Close-RecoveryFailure $state ('enabled accounting write failed; bounded close: ' + $_.Exception.Message))
        }

        # read back: same SID, enabled, non-admin (facts re-read, never assumed)
        try {
            $liveAfter = & $AccountRunner['QueryAccountBySid'] $FixedSid @{}
        } catch {
            return (Close-RecoveryFailure $state ('post-enable SID query threw: ' + $_.Exception.Message))
        }
        if ($liveAfter.present -ne $true -or [string]$liveAfter.sid -ne $FixedSid) {
            return (Close-RecoveryFailure $state ('post-enable SID mismatch/unconfirmed: ' + $(if ($liveAfter.present -eq $null) { [string]$liveAfter.error } else { 'SID ' + [string]$liveAfter.sid })))
        }
        if ($liveAfter.enabled -ne $true) {
            return (Close-RecoveryFailure $state ('post-enable account is not enabled'))
        }
        if ($liveAfter.is_admin -ne $false) {
            return (Close-RecoveryFailure $state ('post-enable account is admin/indeterminate (is_admin=' + [string]$liveAfter.is_admin + ')'))
        }

        # task-before gate: no same-name task exists at registration time
        try {
            $taskBefore = & $AccountRunner['QueryTask'] $ShareTaskName @{}
        } catch {
            return (Close-RecoveryFailure $state ('task query threw: ' + $_.Exception.Message))
        }
        if ($taskBefore.present -eq $null) {
            return (Close-RecoveryFailure $state ('task query unconfirmed: ' + [string]$taskBefore.error))
        }
        if ($taskBefore.present -eq $true) {
            return (Close-RecoveryFailure $state 'a same-name share task exists before recovery registration; refusing overwrite (no takeover)')
        }

        # definition bound to the FIXED SID (never a guess)
        try {
            $definitionXml = [string](& $DefinitionBuilder ([string]$FixedSid))
        } catch {
            return (Close-RecoveryFailure $state ('share task definition build threw: ' + $_.Exception.Message))
        }
        if (-not $definitionXml) {
            return (Close-RecoveryFailure $state 'share task definition could not be built with the fixed SID')
        }
        try {
            # R147: PS 5.1 parses `New-Object SomeType(a, $b).Member` so that `.Member`
            # is swallowed and the expression yields the OBJECT (its [string] is the
            # literal type name) - the registration previously received
            # 'System.Net.NetworkCredential' instead of the password, so Task
            # Scheduler validated a WRONG password (4625 SubStatus 0xC000006A).
            # The explicit parens apply .Password to the constructed instance.
            $plainTmp = (New-Object System.Net.NetworkCredential('', $password)).Password
        } catch {
            return (Close-RecoveryFailure $state ('the ephemeral credential could not be materialized for the registration call: ' + $_.Exception.Message))
        }
        try {
            $reg = & $AccountRunner['RegisterShareTask'] $ShareTaskName $taskPathRooted $definitionXml $AccountName $plainTmp @{}
        } catch {
            $reg = @{ ok = $false; error = ('registration call threw: ' + $_.Exception.Message) }
        }
        if (-not $reg.ok) {
            $state['task_register_result'] = 'failed'
            return (Close-RecoveryFailure $state ('task registration failed; the ephemeral credential is dropped (no regeneration): ' + [string]$reg.error))
        }

        # post-register EXPORT verification (returned ok never substitutes)
        try {
            $taskAfter = & $AccountRunner['QueryTask'] $ShareTaskName @{}
        } catch {
            return (Close-RecoveryFailure $state ('post-register task query threw: ' + $_.Exception.Message))
        }
        if ($taskAfter.present -ne $true) {
            return (Close-RecoveryFailure $state ('post-register export verification failed: task ' + $(if ($taskAfter.present -eq $null) { 'unconfirmed: ' + $taskAfter.error } else { 'absent' })))
        }
        $taskSpecAfter = @{ Name = $ShareTaskName; IdentitySid = $FixedSid; LogonType = 'Password'; Command = $taskCommand }
        $shapeAfter = Test-D4TaskDefinitionShape -DefinitionXml ([string]$taskAfter.xml) `
            -Task $taskSpecAfter -Trigger 'ONDEMAND' -LogonType ([string]$taskSpecAfter['LogonType'])
        if ($shapeAfter.State -ne 'owned') {
            return (Close-RecoveryFailure $state ('post-register export verification failed the FULL shape (' + $shapeAfter.State + ': ' + $shapeAfter.Reason + ')'))
        }
    } finally {
        $plainTmp = $null
        $password = $null
    }
    if (-not (Step-RecoveryStatus $state 'task-registered')) {
        return (Close-RecoveryFailure $state ('illegal recovery transition -> task-registered (no fake rollback)'))
    }
    $state['task_registered_at'] = (Get-Date).ToUniversalTime().ToString('o')
    $state['task_register_result'] = 'registered'
    try {
        [void](Save-D4BootstrapState -StatePath $RecoveryStatePath -State $state -FileBoundary $FileBoundary)
    } catch {
        # §116.4: registration succeeded but the final accounting write failed -
        # the bounded close still disables by the fixed SID (no duplicate
        # register/rotation; the disabled terminal is only recorded when the
        # post-verify holds).
        return (Close-RecoveryFailure $state ('the task-registered accounting write failed; the fixed SID is disabled by the close: ' + $_.Exception.Message))
    }

    return @{
        Ok = $true; Refused = $null
        Reason = 'authorized recovery complete: password reset and enable by the fixed SID, share task registered with an ephemeral credential (not persisted); exported definition verified'
        Status = 'task-registered'; CreatedSid = [string]$FixedSid; config_json = $null
    }
}

function Invoke-D4RecoveryClose {
    # §116.4: the recovery CLOSING path - shared by the recovery failure
    # branches (bounded disable after ANY enable-attempt) and the authorized
    # experiment-end closing (status task-registered). Disables ONLY by the
    # fixed SID through the audited DisableAccount boundary and writes
    # disabled/disabled_at ONLY after the SAME SID reads back Enabled=False.
    # A disable failure, an unverified post-read or a record-write failure
    # NEVER claims 'disabled' (the original reason stays). No second reset.
    param(
        [hashtable]$Contract,
        [string]$RunId,
        [string]$AccountName,
        [string]$TaskName,
        [string]$FixedSid,
        [string]$RecoveryStatePath,
        [string]$PackageHash,
        [string]$PlanIdentity,
        [hashtable]$AccountRunner,
        [hashtable]$FileBoundary,
        [scriptblock]$StoreProbe,
        [scriptblock]$AclProbe,
        [string]$PreserveReason = ''
    )
    if (-not $AccountRunner -or -not $AccountRunner.ContainsKey('DisableAccount') -or
        -not $AccountRunner.ContainsKey('QueryAccountBySid')) {
        throw 'd4-account-bootstrap: recovery close requires the DisableAccount and QueryAccountBySid runner ops'
    }
    if (-not $FixedSid -or $FixedSid -notmatch '^S-1-5-\d+(-\d+)*$') {
        return @{ ok = $false; reason = 'recovery close refuses a malformed SID' }
    }
    $trust = Test-D4BootstrapStoreTrust -BootstrapRoot $Contract['BootstrapRoot'] `
        -StatePath $RecoveryStatePath -AclProbe $AclProbe -StoreProbe $StoreProbe `
        -FileBoundary $FileBoundary -CheckLeaf
    if (-not $trust.trusted) {
        return @{ ok = $false; reason = ('recovery close: the store/record is NOT trusted: ' + $trust.reason) }
    }
    $rec = $null
    try {
        $rec = Read-D4RecoveryState -StatePath $RecoveryStatePath -RunId $RunId `
            -AccountName $AccountName -TaskName $TaskName -PackageHash $PackageHash `
            -PlanIdentity $PlanIdentity -FileBoundary $FileBoundary
    } catch {
        return @{ ok = $false; reason = ('recovery close: recovery record unreadable: ' + $_.Exception.Message) }
    }
    if ($null -eq $rec) {
        return @{ ok = $false; reason = 'recovery close: no recovery record exists for this run; nothing to close' }
    }
    $cs = ''
    $cp = $rec.psobject.Properties['created_sid']
    if ($cp) { $cs = [string]$cp.Value }
    if ([string]::IsNullOrEmpty($cs) -or $cs -ne $FixedSid) {
        return @{ ok = $false; reason = 'recovery close: record created_sid does not equal the fixed SID; no disable' }
    }
    try {
        $d = & $AccountRunner['DisableAccount'] $FixedSid @{}
    } catch {
        return @{ ok = $false; reason = ('recovery close: DisableAccount threw: ' + $_.Exception.Message) }
    }
    if (-not $d.ok) {
        return @{ ok = $false; reason = ('recovery close: DisableAccount failed: ' + [string]$d.error) }
    }
    try {
        $post = & $AccountRunner['QueryAccountBySid'] $FixedSid @{}
    } catch {
        return @{ ok = $false; reason = ('recovery close: post-verify query threw: ' + $_.Exception.Message) }
    }
    if ($post.present -ne $true -or [string]$post.sid -ne $FixedSid -or $post.enabled -ne $false) {
        return @{ ok = $false; reason = ('recovery close: post-verify did NOT read Enabled=False for the same SID (present=' + [string]$post.present + ', enabled=' + [string]$post.enabled + '); NOT recording disabled') }
    }
    $stateBox = @{}
    foreach ($p in $rec.PSObject.Properties) { $stateBox[$p.Name] = $p.Value }
    $stateBox['status'] = 'disabled'
    $stateBox['disabled_at'] = (Get-Date).ToUniversalTime().ToString('o')
    if ($PreserveReason) { $stateBox['unconfirmed_reason'] = $PreserveReason }
    try {
        [void](Save-D4BootstrapState -StatePath $RecoveryStatePath -State $stateBox -FileBoundary $FileBoundary)
    } catch {
        return @{ ok = $false; reason = ('recovery close: the disabled terminal could not be persisted: ' + $_.Exception.Message) }
    }
    return @{ ok = $true; reason = 'recovery close: account disabled by the fixed SID and post-verified (Enabled=False); record marked disabled' }
}

