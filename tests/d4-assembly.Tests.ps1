# Offline tests for execute-d4-experiment-lib.ps1 + the entry's orchestration
# (handoff 64.3-2: current signatures; R62-A/B/C/D acceptance). Everything
# runs on an in-memory fake OS + FAKE runners - no admin rights, no real task
# scheduler, no real ACL change, no experiment root creation. Source strings
# are English-only; the §107.2 adversarial throw messages intentionally
# contain CJK ("未找到") to prove absence is NEVER classified by message
# keywords - the file is saved UTF-8 with BOM, so PS 5.1 decodes it safely.

$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot '..\um-share-deploy\execute-d4-experiment.ps1'
. (Join-Path $PSScriptRoot '..\um-share-deploy\execute-d4-experiment-lib.ps1')
. (Join-Path $PSScriptRoot '..\um-share-deploy\d4-account-bootstrap.ps1')
$script:failures = 0

function Assert-True {
    param([object]$Condition, [string]$Name)
    if ($Condition) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name" -ForegroundColor Red
}

function Assert-Equal {
    param([object]$Actual, [object]$Expected, [string]$Name)
    if ([object]::Equals($Actual, $Expected)) {
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: expected '$Expected', got '$Actual'" -ForegroundColor Red
}

function Assert-Throws {
    param([scriptblock]$Block, [string]$Name, [string]$ExpectInMessage)
    try {
        & $Block | Out-Null
    } catch {
        if ($ExpectInMessage -and ("$($_.Exception.Message)" -notlike "*$ExpectInMessage*")) {
            $script:failures++
            Write-Host "FAIL $Name`: message lacks '$ExpectInMessage': $($_.Exception.Message)" -ForegroundColor Red
            return
        }
        Write-Host "PASS $Name" -ForegroundColor Green
        return
    }
    $script:failures++
    Write-Host "FAIL $Name`: expected a throw" -ForegroundColor Red
}

function New-D4InMemoryOs {
    # In-memory fake OS for the entry-level orchestration tests: every path
    # under $Root (and any $ExtraRoot, e.g. the §93 bootstrap evidence root)
    # lives in the Files/Dirs maps; every other path (the reviewed source
    # tree + the runtime source dir) delegates to the REAL filesystem.
    # Writes/Removes are recorded so zero-write guarantees (R62-B S0 refusal)
    # and the S1 write order can be asserted. No real system object is touched.
    param([string]$Root, [string[]]$ExtraRoots = @())
    $state = @{
        Files   = @{}
        Dirs    = @{}
        Binaries = @{}
        Writes  = New-Object System.Collections.Generic.List[string]
        Removes = New-Object System.Collections.Generic.List[string]
    }
    $roots = @($Root) + @($ExtraRoots)
    $InsideFn = {
        param([string]$Path)
        foreach ($r in $roots) {
            if ($Path.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        }
        return $false
    }.GetNewClosure()
    $boundary = @{
        Exists = {
            param([string]$Path)
            $key = $Path.TrimEnd('\').ToLowerInvariant()
            if ($state['Files'].ContainsKey($key) -or $state['Dirs'].ContainsKey($key) -or
                    $state['Binaries'].ContainsKey($key)) { return $true }
            if (-not (& $InsideFn $Path)) {
                try { return (Test-Path -LiteralPath $Path) } catch { return $false }
            }
            return $false
        }.GetNewClosure()
        IsLeaf = {
            param([string]$Path)
            $key = $Path.TrimEnd('\').ToLowerInvariant()
            if ($state['Files'].ContainsKey($key) -or $state['Binaries'].ContainsKey($key)) { return $true }
            if ($state['Dirs'].ContainsKey($key)) { return $false }
            if (-not (& $InsideFn $Path)) {
                try { return (Test-Path -LiteralPath $Path -PathType Leaf) } catch { return $false }
            }
            return $false
        }.GetNewClosure()
        List = {
            param([string]$Path)
            if (& $InsideFn $Path) {
                $prefix = $Path.TrimEnd('\').ToLowerInvariant() + '\'
                $children = New-Object System.Collections.Generic.List[string]
                foreach ($k in @($state['Files'].Keys) + @($state['Dirs'].Keys) + @($state['Binaries'].Keys)) {
                    if ($k.StartsWith($prefix)) {
                        $rest = $k.Substring($prefix.Length)
                        if ($rest -and -not $rest.Contains('\')) {
                            $children.Add($Path.TrimEnd('\') + '\' + $rest)
                        }
                    }
                }
                return @($children)
            }
            if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return @() }
            return @(Get-ChildItem -LiteralPath $Path -Force | ForEach-Object { $_.FullName })
        }.GetNewClosure()
        ReadText = {
            param([string]$Path)
            $key = $Path.TrimEnd('\').ToLowerInvariant()
            if ($state['Files'].ContainsKey($key)) { return $state['Files'][$key] }
            if (& $InsideFn $Path) {
                throw ('fake read of missing root file: ' + $Path)
            }
            return [IO.File]::ReadAllText($Path)
        }.GetNewClosure()
        WriteText = {
            param([string]$Path, [string]$Content)
            if (-not (& $InsideFn $Path)) {
                throw ('fake write outside the experiment root: ' + $Path)
            }
            $state['Files'][$Path.TrimEnd('\').ToLowerInvariant()] = $Content
            $state['Writes'].Add($Path)
        }.GetNewClosure()
        WriteXml = {
            param([string]$Path, [string]$Content)
            if (-not (& $InsideFn $Path)) {
                throw ('fake xml-write outside the experiment root: ' + $Path)
            }
            $state['Files'][$Path.TrimEnd('\').ToLowerInvariant()] = $Content
            $state['Writes'].Add($Path)
        }.GetNewClosure()
        MakeDir = {
            param([string]$Path)
            $state['Dirs'][$Path.TrimEnd('\').ToLowerInvariant()] = $true
        }.GetNewClosure()
        Remove = {
            param([string]$Path)
            $key = $Path.TrimEnd('\').ToLowerInvariant()
            [void]$state['Files'].Remove($key)
            [void]$state['Dirs'].Remove($key)
            [void]$state['Binaries'].Remove($key)
            $state['Removes'].Add($Path)
        }.GetNewClosure()
        CopyBytes = {
            param([string]$Source, [string]$Destination)
            # Byte-exact fake copy: binaries live in a dedicated byte store
            # (never round-tripped through the content-string map). A source
            # outside the fake root (the real reviewed tree) is read as real
            # bytes; a destination that already exists refuses (no takeover)
            # in BOTH branches (parity with the real boundary).
            $srcKey = $Source.TrimEnd('\').ToLowerInvariant()
            $dstKey = $Destination.TrimEnd('\').ToLowerInvariant()
            if ($state['Files'].ContainsKey($dstKey) -or $state['Dirs'].ContainsKey($dstKey) -or
                    $state['Binaries'].ContainsKey($dstKey)) {
                throw ('copy destination already exists (no takeover): ' + $Destination)
            }
            if ($state['Binaries'].ContainsKey($srcKey)) {
                $state['Binaries'][$dstKey] = $state['Binaries'][$srcKey]
                $state['Writes'].Add($Destination)
                return
            }
            if (-not (& $InsideFn $Source)) {
                $state['Binaries'][$dstKey] = [IO.File]::ReadAllBytes($Source)
                $state['Writes'].Add($Destination)
                return
            }
            throw ('fake CopyBytes: source not found: ' + $Source)
        }.GetNewClosure()
        Hash = {
            param([string]$Path)
            $key = $Path.TrimEnd('\').ToLowerInvariant()
            if ($state['Binaries'].ContainsKey($key)) {
                $sha = [Security.Cryptography.SHA256]::Create()
                try { return ([BitConverter]::ToString($sha.ComputeHash($state['Binaries'][$key])) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
            }
            if ($state['Files'].ContainsKey($key)) {
                $sha = [Security.Cryptography.SHA256]::Create()
                try {
                    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($state['Files'][$key]))) -replace '-', '').ToLowerInvariant()
                } finally { $sha.Dispose() }
            }
            if (-not (& $InsideFn $Path)) {
                return (Get-FileSha256 $Path)
            }
            throw ('fake hash of missing root file: ' + $Path)
        }.GetNewClosure()
    }
    return @{ State = $state; Boundary = $boundary }
}

function New-D4FakeSchtasks {
    # Fake task scheduler: an explicitly absent task is the ONLY absence;
    # /Create stores the definition read through the fake OS boundary; /End
    # and /Delete are recorded. A foreign definition can be injected for the
    # identity negatives.
    param([hashtable]$Oses)
    $st = @{
        Calls = New-Object System.Collections.Generic.List[string]
        Tasks = @{}          # task name (lower) -> definition xml text
        Ended = @{}
        Deleted = @{}
        CreateOk = $true
        QueryFailed = $false
    }
    return @{
        State = $st
        Runner = {
            param([string[]]$argv)
            $action = ($argv | Select-Object -Skip 1 | Select-Object -First 1)
            $st['Calls'].Add(($argv -join ' '))
            $tn = ''
            for ($i = 0; $i -lt $argv.Count; $i++) {
                if ($argv[$i] -eq '/TN' -and $i + 1 -lt $argv.Count) { $tn = $argv[$i + 1]; break }
            }
            if (-not $tn) { return (New-TestRunResult -ExitCode 1 -Output @('no task name')) }
            $key = $tn.ToLowerInvariant()
            if ($action -eq '/Create') {
                if (-not $st['CreateOk']) { return (New-TestRunResult -ExitCode 1 -Output @('create failed')) }
                $xmlPath = ''
                for ($i = 0; $i -lt $argv.Count; $i++) {
                    if ($argv[$i] -eq '/XML' -and $i + 1 -lt $argv.Count) { $xmlPath = $argv[$i + 1]; break }
                }
                if ($xmlPath) {
                    # schtasks consumes the XML file natively: the install
                    # writes the definition as UTF-16LE WITHOUT a BOM and
                    # plain StreamReader heuristics can fall back to
                    # per-byte characters on long files - when the boundary
                    # read shows the NUL-per-byte signature, re-decode the
                    # real bytes the way the OS scheduler does.
                    $content = Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'ReadText' -Arguments @($xmlPath)
                    if ($content -match "`0") {
                        $bytes = [IO.File]::ReadAllBytes($xmlPath)
                        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0x3C -and $bytes[1] -eq 0x00) {
                            $content = [Text.Encoding]::Unicode.GetString($bytes)
                        }
                    }
                    $st['Tasks'][$key] = $content
                }
                return (New-TestRunResult -ExitCode 0 -Output @('SUCCESS'))
            }
            if ($action -eq '/End') {
                $st['Ended'][$key] = $true
                return (New-TestRunResult -ExitCode 0 -Output @('ended'))
            }
            if ($action -eq '/Delete') {
                $st['Deleted'][$key] = $true
                [void]$st['Tasks'].Remove($key)
                return (New-TestRunResult -ExitCode 0 -Output @('deleted'))
            }
            if ($action -eq '/Run') {
                return (New-TestRunResult -ExitCode 0 -Output @('started'))
            }
            if ($action -eq '/Query') {
                if ($st['QueryFailed']) {
                    return (New-TestRunResult -ExitCode 1 -Output @('ERROR: Access is denied.'))
                }
                if (-not $st['Tasks'].ContainsKey($key)) {
                    return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
                }
                if ($argv -contains '/XML') {
                    return (New-TestRunResult -ExitCode 0 -Output @($st['Tasks'][$key]))
                }
                return (New-TestRunResult -ExitCode 0 -Output @('"t","2026-09-09","Ready"'))
            }
            return (New-TestRunResult -ExitCode 1 -Output @('unexpected'))
        }.GetNewClosure()
    }
}

function New-D4FakeRuleStore {
    # R67-A/B fake WFP filter store: models what the OS reports for the
    # experiment's limit rules. 'CreatedFacts' maps id -> the structured
    # facts THIS run's create would land (the strong-verified semantics);
    # 'SeedForeign' maps id -> pre-existing FOREIGN facts (a tampered or
    # foreign same-key rule). Verbs mirror the real runner contract:
    # verify/create/remove/list with structured FACTS and exit 3 for a
    # foreign takeover refusal.
    param([hashtable]$CreatedFacts, [hashtable]$SeedForeign = @{},
          [hashtable]$SeedLive = @{})
    $st = @{
        Calls = New-Object System.Collections.Generic.List[string]
        CreatedFacts = $CreatedFacts
        Live = @{}
    }
    foreach ($k in $SeedForeign.Keys) {
        $st['Live'][$k.ToLowerInvariant()] = [string]$SeedForeign[$k]
    }
    foreach ($k in $SeedLive.Keys) {
        $st['Live'][$k.ToLowerInvariant()] = [string]$SeedLive[$k]
    }
    return @{
        State = $st
        Runner = {
            param([string[]]$argv)
            $verb = $argv[1]
            $st['Calls'].Add(($argv -join ' '))
            $out = @()
            $exit = 0
            foreach ($id in $argv | Select-Object -Skip 2) {
                $key = $id.ToLowerInvariant()
                $present = $st['Live'].ContainsKey($key)
                $foreign = $present -and (-not $st['CreatedFacts'].ContainsKey($key) -or
                                          $st['Live'][$key] -ne $st['CreatedFacts'][$key])
                if ($verb -eq 'list') {
                    if ($present) { $out += ('PRESENT ' + $id) } else { $out += ('ABSENT ' + $id) }
                    continue
                }
                if ($verb -eq 'verify') {
                    if ($present) { $out += ('PRESENT ' + $id + ' ' + $st['Live'][$key]) }
                    else { $exit = 1; $out += ('ABSENT ' + $id) }
                    continue
                }
                if ($verb -eq 'create') {
                    if ($present) {
                        if ($foreign) { $exit = 3; $out += ('FOREIGN ' + $id + ': live filter does not match this run''s specification') }
                        else { $out += ('ALREADY-PRESENT ' + $id + ' (facts match; idempotent)') }
                        continue
                    }
                    if (-not $st['CreatedFacts'].ContainsKey($key)) {
                        $exit = 2; $out += ('CREATE-FAILED no reviewed spec facts for ' + $id); continue
                    }
                    $st['Live'][$key] = $st['CreatedFacts'][$key]
                    $out += ('CREATED ' + $id)
                    continue
                }
                if ($verb -eq 'remove') {
                    if (-not $present) { $out += ('ABSENT ' + $id + ' (idempotent)'); continue }
                    if ($foreign) { $exit = 3; $out += ('FOREIGN ' + $id + ': live filter does not match this run''s specification'); continue }
                    [void]$st['Live'].Remove($key)
                    $out += ('REMOVED ' + $id)
                    continue
                }
                $exit = 2; $out += ('unsupported limits verb: ' + $verb)
            }
            return (New-TestRunResult -ExitCode $exit -Output $out)
        }.GetNewClosure()
    }
}

function New-D4FakeNativeOps {
    # R78-D fake NATIVE surface for the REAL runner's decision tests: an
    # in-memory BFE model that stores REAL codec-encoded 200-byte filter
    # images (the same bytes FwpmFilterAdd0 would persist). The seven-key
    # surface mirrors the fwpuclnt calls; the runner then decodes the stored
    # image through the REAL codec - no pre-canned facts strings anywhere.
    # Every allocation the surface hands out is freed by the surface's own
    # Free (the runner's finally contract). The rc constants are captured as
    # LOCALS (same unsigned values the runner matches - never the hex
    # literals, which PS 5.1 parses as negative Int32).
    $rcNotFound = $script:D4FwpRcFilterNotFound
    $rcAlready = $script:D4FwpRcAlreadyExists
    $st = @{
        Store  = @{}
        Owned  = @{}
        Engine = [IntPtr]24
        Calls  = New-Object System.Collections.Generic.List[string]
    }
    $norm = {
        # Deterministic app-id normalization (BFE normalizes too; the
        # runner only requires that the expected bytes and the stored
        # filter's app-id condition carry the SAME bytes).
        param([string]$Path)
        return [Text.Encoding]::Unicode.GetBytes($Path.ToLowerInvariant())
    }
    return @{
        State = $st
        Ops = @{
            EngineOpen = {
                param([ref]$Engine)
                $st['Calls'].Add('EngineOpen')
                $Engine.Value = $st['Engine']
                return [uint32]0
            }.GetNewClosure()
            GetAppId = {
                param([string]$AppPath, [ref]$Blob)
                $st['Calls'].Add('GetAppId')
                $bytes = & $norm $AppPath
                $dataPtr = [Runtime.InteropServices.Marshal]::AllocHGlobal([Math]::Max(1, $bytes.Length))
                [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $dataPtr, $bytes.Length)
                # NOTE: the local MUST NOT collide with the [ref]$Blob
                # parameter name (case-insensitive) - inside a GetNewClosure
                # scriptblock a same-name rebind does not take effect and the
                # PSReference leaks into the Marshal calls.
                $blobPtr = [Runtime.InteropServices.Marshal]::AllocHGlobal(16)
                [Runtime.InteropServices.Marshal]::WriteInt32($blobPtr, 0, $bytes.Length)
                [Runtime.InteropServices.Marshal]::WriteIntPtr($blobPtr, 8, $dataPtr)
                $Blob.Value = $blobPtr
                return [uint32]0
            }.GetNewClosure()
            GetByKey = {
                param([IntPtr]$Engine, [ref]$Key, [ref]$Filter)
                $st['Calls'].Add('GetByKey ' + $Key.Value.ToString())
                $k = $Key.Value.ToString().ToLowerInvariant()
                if (-not $st['Store'].ContainsKey($k)) {
                    $Filter.Value = [IntPtr]::Zero
                    return $rcNotFound   # the BFE 'not found' code (0x80320003, unsigned)
                }
                $owned = [Runtime.InteropServices.Marshal]::ReadIntPtr($st['Store'][$k], 0)
                $imageBytes = New-Object byte[] 200
                [Runtime.InteropServices.Marshal]::Copy($owned, $imageBytes, 0, 200)
                $p = [Runtime.InteropServices.Marshal]::AllocHGlobal(200)
                [Runtime.InteropServices.Marshal]::Copy($imageBytes, 0, $p, 200)
                $Filter.Value = $p
                return [uint32]0
            }.GetNewClosure()
            Add = {
                param([IntPtr]$Engine, $Native, [ref]$NewId)
                $bytes = New-Object byte[] 200
                [Runtime.InteropServices.Marshal]::Copy($Native.Buffer, $bytes, 0, 200)
                $keyBytes = New-Object byte[] 16
                [Array]::Copy($bytes, 0, $keyBytes, 0, 16)
                $k = [System.Guid]::new($keyBytes).ToString().ToLowerInvariant()
                $st['Calls'].Add('Add ' + $k)
                if ($st['Store'].ContainsKey($k)) {
                    return $rcAlready   # the BFE 'already exists' code (0x80320009, unsigned)
                }
                # BFE DEEP-COPIES the filter and its pointees at Add time and
                # the caller disposes the native buffer immediately after - a
                # shallow 200-byte copy would leave the cond-array/blob
                # pointers dangling into freed memory. The clone re-allocates
                # every pointee and re-points the stored image at its own
                # blocks (kept alive per entry; Delete frees them).
                $header = [Runtime.InteropServices.Marshal]::AllocHGlobal(200)
                [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $header, 200)
                $owns = New-Object System.Collections.Generic.List[IntPtr]
                $owns.Add($header)
                $condCount = [Runtime.InteropServices.Marshal]::ReadInt32($header, 0x70)
                $oldConds = [Runtime.InteropServices.Marshal]::ReadIntPtr($header, 0x78)
                $condLen = [Math]::Max(1, $condCount) * 40
                $condBytes = New-Object byte[] $condLen
                [Runtime.InteropServices.Marshal]::Copy($oldConds, $condBytes, 0, ($condCount * 40))
                $newConds = [Runtime.InteropServices.Marshal]::AllocHGlobal($condLen)
                [Runtime.InteropServices.Marshal]::Copy($condBytes, 0, $newConds, $condLen)
                [Runtime.InteropServices.Marshal]::WriteIntPtr($header, 0x78, $newConds)
                $owns.Add($newConds)
                for ($ci = 0; $ci -lt $condCount; $ci++) {
                    $c = [IntPtr]($newConds.ToInt64() + ($ci * 40))
                    if ([Runtime.InteropServices.Marshal]::ReadInt32($c, 0x18) -eq 12) {
                        # BYTE_BLOB condition: clone the blob wrapper AND its
                        # data, then re-point wrapper.data and cond.union.
                        $oldWrapper = [Runtime.InteropServices.Marshal]::ReadIntPtr($c, 0x20)
                        $blobSize = [Math]::Max(1, [Runtime.InteropServices.Marshal]::ReadInt32($oldWrapper, 0))
                        $oldData = [Runtime.InteropServices.Marshal]::ReadIntPtr($oldWrapper, 8)
                        $wrapBytes = New-Object byte[] 16
                        [Runtime.InteropServices.Marshal]::Copy($oldWrapper, $wrapBytes, 0, 16)
                        $newWrapper = [Runtime.InteropServices.Marshal]::AllocHGlobal(16)
                        [Runtime.InteropServices.Marshal]::Copy($wrapBytes, 0, $newWrapper, 16)
                        $dataBytes = New-Object byte[] $blobSize
                        [Runtime.InteropServices.Marshal]::Copy($oldData, $dataBytes, 0, $blobSize)
                        $newData = [Runtime.InteropServices.Marshal]::AllocHGlobal($blobSize)
                        [Runtime.InteropServices.Marshal]::Copy($dataBytes, 0, $newData, $blobSize)
                        [Runtime.InteropServices.Marshal]::WriteIntPtr($newWrapper, 8, $newData)
                        [Runtime.InteropServices.Marshal]::WriteIntPtr($c, 0x20, $newWrapper)
                        $owns.Add($newWrapper)
                        $owns.Add($newData)
                    }
                }
                $storeEntry = [Runtime.InteropServices.Marshal]::AllocHGlobal([IntPtr]::Size)
                [Runtime.InteropServices.Marshal]::WriteIntPtr($storeEntry, 0, $header)
                $st['Store'][$k] = $storeEntry
                $st['Owned'][$k] = $owns
                $NewId.Value = [uint64]$st['Store'].Count
                return [uint32]0
            }.GetNewClosure()
            Delete = {
                param([IntPtr]$Engine, [ref]$Key)
                $st['Calls'].Add('Delete ' + $Key.Value.ToString())
                $k = $Key.Value.ToString().ToLowerInvariant()
                if ($st['Owned'].ContainsKey($k)) {
                    foreach ($blk in $st['Owned'][$k]) {
                        [void][Runtime.InteropServices.Marshal]::FreeHGlobal($blk)
                    }
                    [void]$st['Owned'].Remove($k)
                }
                [void]$st['Store'].Remove($k)
                return [uint32]0
            }.GetNewClosure()
            Free = {
                param([ref]$Ptr)
                if ($Ptr.Value -ne [IntPtr]::Zero) {
                    [void][Runtime.InteropServices.Marshal]::FreeHGlobal($Ptr.Value)
                    $Ptr.Value = [IntPtr]::Zero
                }
                return [uint32]0
            }.GetNewClosure()
            Close = {
                param([IntPtr]$Engine)
                $st['Calls'].Add('Close')
                return [uint32]0
            }.GetNewClosure()
        }
    }
}

function New-D4FakeProbe {
    # R80 fake probe collector: the subcommands RETURN the reviewed verdict
    # texts AND ACTUALLY WRITE the artifacts they stand for (a probe that
    # only prints success text is not lifecycle evidence): the E3 publish
    # chain rewrites the projection twice and creates the isolated store;
    # the E4 fault matrix changes both again. -FailAfterWrites makes the
    # lifecycle mutate first and THEN fail (the freeze-interruption fixture).
    param([hashtable]$Oses, [hashtable]$Contract, [switch]$FailAfterWrites)
    $st = @{ Calls = New-Object System.Collections.Generic.List[string] }
    $root = [string]$Contract['ExperimentRoot']
    $projRel = [string]$Contract['ProjectionRel']
    return @{
        State = $st
        Runner = {
            param([string[]]$argv)
            $st['Calls'].Add(($argv -join ' '))
            $cmd = $argv[1]
            if ($cmd -eq 'pipe-probe') {
                # the placeholder-SDDL negative MUST fail (E1's negative-first gate)
                return (New-TestRunResult -ExitCode 1 -Output @('pipe creation refused (placeholder SDDL)'))
            }
            if ($cmd -eq 'request-matrix') {
                # The REAL runtime writes live status/registration during the
                # matrix window; the fixture materializes the SAME artifacts
                # so the E1 R83 retention + qualified-cleanup closure is
                # exercised offline (W2 run21's three unconfirmed objects).
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root 'broker\broker-status.json'), '{"state": "serving"}')
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root 'run\live\guard-status.json'), '{"guard": "ok"}')
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root 'run\live\instance-registration.json'), '{"instance_id": "offline-fixture"}')
                return (New-TestRunResult -ExitCode 0 -Output @('mgmt-status-ok', 'mgmt-wrong-hmac-refused',
                    'mgmt-replay-refused', 'http-wrong-key-refused'))
            }
            if ($cmd -eq 'stall-matrix') {
                return (New-TestRunResult -ExitCode 0 -Output @('silent-cancelled', 'drip-cancelled',
                    'blocking-cancelled', 'post-timeout-request-ok'))
            }
            if ($cmd -eq 'projection-lifecycle') {
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root $projRel), '{"published": 2, "generation": 2}')
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root $projRel), '{"published": 1, "generation": 3}')
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root 'share-store.db'), 'E3-STORE-BYTES')
                if ($FailAfterWrites) {
                    return (New-TestRunResult -ExitCode 1 -Output @('lifecycle aborted after the publications landed'))
                }
                return (New-TestRunResult -ExitCode 0 -Output @('ack-generation-matched', 'http-key-ok',
                    'wrong-key-401', 'revoked-key-refused'))
            }
            if ($cmd -eq 'fault-injection') {
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root $projRel), '{"published": 1, "generation": 5}')
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    (Join-Path $root 'share-store.db'), 'E4-STORE-BYTES')
                return (New-TestRunResult -ExitCode 0 -Output @('refused', 'recovery-recorded'))
            }
            return (New-TestRunResult -ExitCode 2 -Output @('unsupported probe subcommand: ' + $cmd))
        }.GetNewClosure()
    }
}

function New-D4LifecycleNetPair {
    # R80: the namespace-free gate must see the port EMPTY before any start;
    # the E1 bounded wait must see :9100 LISTENING after /Run; the cleanup's
    # final port/process evidence must see them EMPTY again after /End. ONE
    # shared lifecycle flag wires all THREE observers (the schtasks wrapper +
    # the netstat + the process collector) to the real /Run and /End verbs -
    # a static LISTENING row would refuse every S0 with 'port 9100 has a
    # listener' (a genuinely OCCUPIED port is a separate negative fixture,
    # New-D4FakeOccupiedPort).
    param([scriptblock]$SchRunner, [string]$ExperimentRoot)
    $flag = @{ Started = $false }
    $wrapped = {
        param([string[]]$argv)
        $r = & $SchRunner $argv
        $verb = ($argv | Select-Object -Skip 1 | Select-Object -First 1)
        if ($verb -eq '/Run') { $flag['Started'] = $true }
        if ($verb -eq '/End') { $flag['Started'] = $false }
        return $r
    }.GetNewClosure()
    $net = {
        param([string[]]$argv)
        if ($flag['Started']) {
            return (New-TestRunResult -ExitCode 0 -Output @('  TCP    0.0.0.0:9100     0.0.0.0:0      LISTENING     4242'))
        }
        return (New-TestRunResult -ExitCode 0 -Output @(''))
    }.GetNewClosure()
    $proc = {
        param([string[]]$argv)
        if ($flag['Started']) {
            # Well-formed like the real Get-CimInstance | ConvertTo-Json
            # (backslashes escaped - a raw \P would be an invalid JSON
            # escape and a legitimate Unconfirmed).
            $exe = ($ExperimentRoot + '\um-share-python.exe').Replace('\', '\\')
            $row = '[{"ProcessId":4242,"CommandLine":"run-live.cmd","ExecutablePath":"' + $exe + '"}]'
            return (New-TestRunResult -ExitCode 0 -Output @($row))
        }
        return (New-TestRunResult -ExitCode 0 -Output @(''))
    }.GetNewClosure()
    return @{ Schtasks = $wrapped; Net = $net; Proc = $proc }
}

function New-D4FakeOccupiedPort {
    # R80 negative fixture: a GENUINELY occupied 9100 port - an unrelated
    # live listener independent of THIS run's schtasks lifecycle. The S0
    # namespace-free gate MUST refuse it (the stateful pair proves the
    # occupied branch still refuses instead of always refusing).
    return {
        param([string[]]$argv)
        return (New-TestRunResult -ExitCode 0 -Output @('  TCP    0.0.0.0:9100     0.0.0.0:0      LISTENING     9999'))
    }.GetNewClosure()
}

function ConvertTo-D4LedgerHashtable {
    # A raw ConvertFrom-Json ledger is a PSCustomObject; the direct-ledger
    # edit helpers (Add-D4IntentLedgerEntry) accept the canonical LOWERCASE
    # contract. This converts the raw disk form into that contract for
    # isolated ledger edits (the run's producers use New-D4IntentLedger).
    param([object]$JsonLedger)
    $entries = @()
    foreach ($e in @($JsonLedger.entries)) {
        $h = @{}
        foreach ($p in $e.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $entries += $h
    }
    return @{ run_id = [string]$JsonLedger.run_id; package_hash = [string]$JsonLedger.package_hash; entries = $entries }
}

function New-D4HarnessAclRunner {
    # R67-B harness ACL edge: a stateful icacls simulation whose /save and
    # /restore go through the FAKE file boundary (the backup never touches
    # the real filesystem), /grant:r replaces the granted rows, and a bare
    # query echoes the simulated rows for the Test-AclOutput verdict.
    param([hashtable]$Oses)
    $st = @{ Calls = New-Object System.Collections.Generic.List[string]; Acl = @{} }
    return @{
        State = $st
        Runner = {
            param([string[]]$argv)
            $cmd = $argv[0]
            $path = $argv[1]
            $key = $path.ToLowerInvariant()
            $st['Calls'].Add(($argv -join ' '))
            if (-not $st['Acl'].ContainsKey($key)) { $st['Acl'][$key] = @() }
            if ($argv -contains '/save') {
                Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'WriteText' -Arguments @(
                    $argv[-1], (@($st['Acl'][$key]) -join "`n"))
                return (New-TestRunResult -ExitCode 0 -Output @('saved'))
            }
            if ($argv -contains '/restore') {
                $rows = @(Invoke-D4File -Boundary $Oses['Boundary'] -Operation 'ReadText' -Arguments @($argv[-1]))
                $st['Acl'][$key] = @($rows | Where-Object { $_ })
                return (New-TestRunResult -ExitCode 0 -Output @('restored'))
            }
            if ($argv -contains '/inheritance:r') {
                # A protected DACL: inherited/other ACEs are REMOVED before
                # the explicit grants (the exact matrix decides alone).
                $st['Acl'][$key] = @()
            }
            if ($argv -contains '/remove') {
                # icacls /remove <principal>: remove the ACE rows for that
                # principal - the EXACT inverse of a /grant:r for one target.
                # The principal is resolved by the SAME canonical identity
                # (SID literal or NTAccount->SID) as Test-AclOutput: a name
                # row for the requested SID is matched via the resolver.
                $removed = 0
                $kept = @()
                $target = ''
                for ($i = 2; $i -lt $argv.Count; $i++) {
                    if ($argv[$i] -eq '/remove') { $target = $argv[$i + 1] }
                }
                $targetSid = $null
                if ($target) {
                    if ($target.StartsWith('*')) { $target = $target.Substring(1) }
                    if ([regex]::IsMatch($target, '^S-1-5-')) {
                        try { $targetSid = [string](New-Object System.Security.Principal.SecurityIdentifier($target)).Value } catch { }
                    }
                }
                foreach ($row in @($st['Acl'][$key])) {
                    $m = [regex]::Match($row, '^([^:]+):')
                    if ($m.Success) {
                        $rowPrincipal = $m.Groups[1].Value
                        $match = $false
                        if ($targetSid) {
                            # The stored rows carry the SID as '*S-1-5-...'
                            # (the grant ACE's own form); strip the '*' prefix
                            # before the SAME canonical resolution.
                            $rowText = $rowPrincipal
                            if ($rowText.StartsWith('*')) { $rowText = $rowText.Substring(1) }
                            $res = Resolve-D4TaskPrincipalSid -PrincipalText $rowText
                            if ($res.ok -and [string]$res.sid -eq $targetSid) { $match = $true }
                        } elseif ($rowPrincipal -ieq $target) { $match = $true }
                        if ($match) { $removed++ ; continue }
                    }
                    $kept += $row
                }
                $st['Acl'][$key] = $kept
                return (New-TestRunResult -ExitCode 0 -Output @(('removed:' + $removed)))
            }
            $grants = @()
            for ($i = 2; $i -lt $argv.Count - 1; $i++) {
                if ($argv[$i] -eq '/grant:r') { $grants += $argv[$i + 1] }
            }
            if ($grants.Count -gt 0) {
                # Normalize to the icacls OUTPUT shape: every trailing bare
                # right code gets its own parentheses (the grant syntax
                # '(OI)(CI)F' prints as '(OI)(CI)(F)' on query).
                $rows = @()
                foreach ($ace in $grants) {
                    $idx = $ace.IndexOf(':')
                    $name = $ace.Substring(0, $idx)
                    $flags = [regex]::Replace($ace.Substring($idx + 1), '(?<=\))([A-Z][A-Z,]*)$', '($1)')
                    $rows += ($name + ':' + $flags)
                }
                $st['Acl'][$key] = @($st['Acl'][$key]) + @($rows)
                return (New-TestRunResult -ExitCode 0 -Output @('granted'))
            }
            return (New-TestRunResult -ExitCode 0 -Output (@($path) + @($st['Acl'][$key])))
        }.GetNewClosure()
    }
}

function New-D4EmptyRunner {
    # Exit 0 with empty output: an EMPTY legal result (used for netstat with
    # no listener and for the process query with no matching process).
    param([string]$Tag)
    $st = @{ Calls = New-Object System.Collections.Generic.List[string] }
    return @{
        State = $st
        Runner = {
            param([string[]]$argv)
            $st['Calls'].Add(($argv -join ' '))
            return (New-TestRunResult -ExitCode 0 -Output @(''))
        }.GetNewClosure()
    }
}

function New-D4AbsentTaskRunner {
    # The ONLY legal absence: the explicit "cannot find" evidence.
    return {
        param([string[]]$argv)
        return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
    }.GetNewClosure()
}

function New-D4TestConfig {
    # A VALID fixed-structure experiment config (passes Test-D4ExperimentConfig).
    param([string]$RunId, [string]$RuntimeSourceDir,
          [object[]]$RuntimeFiles, [string]$LogonMethod = 'task-logon',
          [string]$IdentitySource = 'collected')
    $contract = New-D4ExperimentContract
    return @{
        run_id = $RunId
        identity_source = $IdentitySource
        operator_account = 'dacint'
        operator_sid = 'S-1-5-21-3623811015-3361044348-30300820-1001'
        share_account = 'um_share'
        share_sid = 'S-1-5-21-3623811015-3361044348-30300820-1002'
        logon_method = $LogonMethod
        runtime_source_dir = $RuntimeSourceDir
        runtime_files = $RuntimeFiles
        python_home = 'C:\PythonExp'
        broker_pipe_sddl = 'D:P(A;;GA;;;SY)'
        instance_pipe_sddl = 'D:P(A;;GR;;;SY)'
        instance_pipe_prefix = $contract['InstancePipePrefix']
        share_port = 9100
        probe = $true
        auth_mode = 'projection'
        limit_rules = $ruleSpecs
    }
}

$contract = New-D4ExperimentContract
$sourceRoot = $contract['SourceRootDefault']
# R67-A: the fixed experiment isolation rule set (the reviewed pattern):
# permit = app-id + 127.0.0.1:9100, block-v4/block-v6 = app-id ONLY.
$layerV4 = 'c38d57d1-05a7-4c33-904f-7fbceee60e82'
$layerV6 = '4a72393b-319f-44bc-84c3-ba54dcb3b6b4'
$rulePermit = @{ id = '9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55'; layer = 'connect-v4'; action = 'permit';
                 app_path = 'C:\ProgramData\UMShareExp-broker\um-share-python.exe';
                 remote_addr = '127.0.0.1'; remote_port = 9100 }
$ruleBlockV4 = @{ id = 'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66'; layer = 'connect-v4'; action = 'block';
                  app_path = 'C:\ProgramData\UMShareExp-broker\um-share-python.exe' }
$ruleBlockV6 = @{ id = 'b2d5f9a8-3c4a-4e67-9f81-2a0b3c4d5e77'; layer = 'connect-v6'; action = 'block';
                  app_path = 'C:\ProgramData\UMShareExp-broker\um-share-python.exe' }
$ruleSpecs = @($rulePermit, $ruleBlockV4, $ruleBlockV6)
$ruleIds = @($rulePermit['id'], $ruleBlockV4['id'], $ruleBlockV6['id'])
$factPermit = "layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=9100"
$factBlockV4 = "layer=$layerV4 action=0x1001 conds=1 appid=MATCH addr=NA port=NA"
$factBlockV6 = "layer=$layerV6 action=0x1001 conds=1 appid=MATCH addr=NA port=NA"
$createdFacts = @{ $rulePermit['id'] = $factPermit; $ruleBlockV4['id'] = $factBlockV4; $ruleBlockV6['id'] = $factBlockV6 }

$realBoundary = New-D4RealFileBoundary
$tmpBase = Join-Path ([IO.Path]::GetTempPath()) ("d4-assembly-" + [IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tmpBase -Force | Out-Null

# ---- contract purity (§60.3 fixed object table) ----------------------------
Write-Host '== contract: fixed experiment namespace =='
Assert-Equal $contract['ExperimentRoot'] 'C:\ProgramData\UMShareExp-broker' 'experiment root is the fixed object'
Assert-Equal $contract['BrokerTaskName'] 'umshare-broker-exp' 'broker task is the fixed experiment task'
Assert-Equal $contract['ShareTaskName'] 'umshare-share-exp' 'share task is the fixed experiment task'
Assert-Equal $contract['BrokerPipeName'] '\\.\pipe\um-share-broker-exp' 'broker pipe is the fixed experiment pipe'
Assert-Equal $contract['InstancePipePrefix'] '\\.\pipe\um-share-instance-exp-' 'instance prefix is the fixed experiment prefix'
Assert-Equal $contract['SharePort'] 9100 'experiment port is fixed at 9100'
Assert-Equal $contract['TaskTrigger'] 'ONDEMAND' 'experiment tasks are ONDEMAND (no boot trigger)'
Assert-True ($contract['ProductionRoot'] -ne $contract['ExperimentRoot']) 'experiment root differs from the production root'
Assert-Equal @($contract['ModuleClosure']).Count 13 'module closure is the reviewed 13-file set (incl. the bootstrap module)'

# ---- run id format (R62-A) -------------------------------------------------
Write-Host '== run id: bounded format, suggested ids pass =='
Assert-True (Test-D4RunIdFormat '20260909T120000-abcdef12') 'a bounded run id passes the format gate'
Assert-Throws { Test-D4RunIdFormat '' } 'empty run id refuses' 'required'
Assert-Throws { Test-D4RunIdFormat 'a/b' } 'a path character refuses' 'must match'
Assert-Throws { Test-D4RunIdFormat 'short' } 'a too-short run id refuses' 'must match'
Assert-True (Test-D4RunIdFormat (New-D4SuggestedRunId)) 'a suggested run id always passes the format gate'

# ---- isolation (§60.4) -----------------------------------------------------
Write-Host '== isolation: fixed root, reparse chain, production collisions =='
$nullProbe = { param([string]$Path) return $null }
Assert-True (Test-D4Isolation -Contract $contract -ResolvedRoot $contract['ExperimentRoot'] -ItemProbe $nullProbe) 'fixed root with clean ancestors is isolated'
Assert-Throws { Test-D4Isolation -Contract $contract -ResolvedRoot 'C:\ProgramData\UMShareExp-other' -ItemProbe $nullProbe } 'a different root refuses' 'FIXED'
Assert-Throws { Test-D4Isolation -Contract $contract -ResolvedRoot 'C:\ProgramData\UMShareRun' -ItemProbe $nullProbe } 'production root itself refuses' 'is FIXED to'
$reparseAncestor = {
    param([string]$Path)
    if ($Path -eq 'C:\ProgramData') {
        return (New-Object PSObject -Property @{ Attributes = [IO.FileAttributes]::ReparsePoint })
    }
    return $null
}
Assert-Throws { Test-D4Isolation -Contract $contract -ResolvedRoot $contract['ExperimentRoot'] -ItemProbe $reparseAncestor } 'ancestor reparse point refuses' 'ancestor reparse point refused'
$reparseRoot = {
    param([string]$Path)
    if ($Path -eq $contract['ExperimentRoot']) {
        return (New-Object PSObject -Property @{ Attributes = [IO.FileAttributes]::ReparsePoint })
    }
    return $null
}
Assert-Throws { Test-D4Isolation -Contract $contract -ResolvedRoot $contract['ExperimentRoot'] -ItemProbe $reparseRoot } 'root reparse point refuses' 'reparse point'

# ---- closure manifest (real reviewed sources, read-only) --------------------
Write-Host '== closure manifest: hashes from the real reviewed sources =='
$manifest = Get-D4SourceClosureManifest -Contract $contract -SourceRoot $sourceRoot -FileBoundary $realBoundary
Assert-Equal @($manifest['Entries']).Count 13 'manifest covers the whole closure (incl. the bootstrap module)'
$manifest2 = Get-D4SourceClosureManifest -Contract $contract -SourceRoot $sourceRoot -FileBoundary $realBoundary
Assert-Equal @($manifest2['Entries']).Count @($manifest['Entries']).Count 'manifest is stable across calls'
$emptySrc = Join-Path $tmpBase 'src-empty'
New-Item -ItemType Directory -Path $emptySrc -Force | Out-Null
Assert-Throws { Get-D4SourceClosureManifest -Contract $contract -SourceRoot $emptySrc -FileBoundary $realBoundary } 'missing closure file refuses the manifest' 'source closure file missing'

# ---- package hash binds code + runtime selection (R62-A) ---------------------
Write-Host '== package hash: code bodies + runtime manifest are identity =='
$pkgA = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson 'C:\py|C:\py|a.dll=aa'
$pkgB = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson 'C:\py|C:\py|a.dll=bb'
Assert-True ($pkgA -ne $pkgB) 'a changed runtime manifest changes the package hash'
$pkgC = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson 'C:\py2|C:\py|a.dll=aa'
Assert-True ($pkgA -ne $pkgC) 'a changed runtime source dir changes the package hash'

# ---- object inventory (complete, root-scoped, Config+PackageHash) ------------
Write-Host '== inventory: complete object list before any action =='
$runId = '20260909T120000-abcdef12'
$rtDir = Join-Path $tmpBase 'runtime'
New-Item -ItemType Directory -Path $rtDir -Force | Out-Null
$rtFile = Join-Path $rtDir 'python-core.bin'
[IO.File]::WriteAllText($rtFile, 'RUNTIME-CORE', (New-Object System.Text.UTF8Encoding($false)))
$rtSha = (Get-FileSha256 $rtFile)
$config = New-D4TestConfig -RunId $runId -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$pkg = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson ([string]$config['runtime_source_dir'] + '|' + [string]$config['python_home'] + '|' + 'python-core.bin=' + $rtSha)
$inventory = New-D4ObjectInventory -Contract $contract -ClosureManifest $manifest -Config $config -PackageHash $pkg
Assert-Equal $inventory['RunId'] $runId 'inventory binds the approved run id'
Assert-Equal $inventory['PackageHash'] $pkg 'inventory binds the package hash'
Assert-Equal $inventory['Root'] $contract['ExperimentRoot'] 'inventory is rooted at the fixed experiment root'
$regEntry = @($inventory['Files']) | Where-Object { $_.Path -like '*instance-registration.json' } | Select-Object -First 1
Assert-True ($regEntry.Path -like '*run\live\instance-registration.json') 'registration self-publish path is run\live'
$stateEntry = @($inventory['Files']) | Where-Object { $_.Kind -eq 'stage-state' } | Select-Object -First 1
Assert-True ($stateEntry.Path -like '*evidence\stage-state-*') 'stage state lives in the evidence dir'
Assert-True ($stateEntry.Path -like ('*stage-state-' + $pkg.Substring(0, 8) + '-' + $runId + '.*')) 'stage state name binds package hash AND run id'
$intentEntry = @($inventory['Files']) | Where-Object { $_.Kind -eq 'intent-ledger' } | Select-Object -First 1
Assert-True ($intentEntry.Path -like ('*intent-' + $pkg.Substring(0, 8) + '-' + $runId + '.*')) 'intent ledger name binds package hash AND run id'
Assert-True (@($inventory['Tasks'] | Where-Object { $_.Trigger -eq 'ONDEMAND' }).Count -eq 2) 'both experiment tasks are ONDEMAND'
Assert-True (@($inventory['Tasks'] | Where-Object { $_.Command.StartsWith($contract['ExperimentRoot']) }).Count -eq 2) 'both task commands reference the experiment root'
$brokerTask = @($inventory['Tasks']) | Where-Object { $_.Name -eq $contract['BrokerTaskName'] } | Select-Object -First 1
Assert-Equal $brokerTask.IdentitySid $contract['SystemSid'] 'broker task identity is SYSTEM (collected contract)'
Assert-Equal $inventory['Port'] 9100 'inventory port is the fixed experiment port'
Assert-True ($inventory['Image'].StartsWith($contract['ExperimentRoot'])) 'the experiment image is the root-local copy'
$rtInventory = @($inventory['Files']) | Where-Object { $_.Kind -eq 'runtime-copy' } | Select-Object -First 1
Assert-True ($rtInventory.Path -like '*python-core.bin') 'runtime copies are listed as owned objects'
Assert-Equal $rtInventory.Sha256 $rtSha 'runtime copy carries the verified source hash'

# ---- experiment config validation (R62-A/D fixed structure) -------------------
Write-Host '== config: fixed structure, placeholder DACL refusal =='
$bad1 = @{} ; foreach ($k in $config.Keys) { $bad1[$k] = $config[$k] }
$bad1.Remove('run_id')
Assert-Throws { Test-D4ExperimentConfig -Config $bad1 -Contract $contract } 'missing run_id refuses' 'run_id'
$bad2 = @{} ; foreach ($k in $config.Keys) { $bad2[$k] = $config[$k] }
$bad2['new_slot'] = 1
Assert-Throws { Test-D4ExperimentConfig -Config $bad2 -Contract $contract } 'an unknown config slot refuses' 'unknown slot'
$bad3 = @{} ; foreach ($k in $config.Keys) { $bad3[$k] = $config[$k] }
$bad3['instance_pipe_sddl'] = 'D:P(A;;FA;;;S-1-5-18-exp)'
Assert-Throws { Test-D4ExperimentConfig -Config $bad3 -Contract $contract } 'a placeholder DACL slot refuses' 'placeholder'
$bad4 = @{} ; foreach ($k in $config.Keys) { $bad4[$k] = $config[$k] }
$bad4['share_port'] = 8100
Assert-Throws { Test-D4ExperimentConfig -Config $bad4 -Contract $contract } 'a non-9100 port refuses' 'must be 9100'
$bad5 = @{} ; foreach ($k in $config.Keys) { $bad5[$k] = $config[$k] }
$bad5['probe'] = $false
Assert-Throws { Test-D4ExperimentConfig -Config $bad5 -Contract $contract } 'probe=false refuses' 'probe must be true'
$config2Unest = New-D4TestConfig -RunId $runId -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) -LogonMethod 'unestablished'
[void](Test-D4ExperimentConfig -Config $config2Unest -Contract $contract)
Assert-True $true 'an unestablished logon method is a legal recorded fact (never guessed)'

# ---- stage state: package hash binding, disorder refusal (R62-A) --------------
Write-Host '== stage state: package-hash/run-id binding, disorder refusal =='
$statePath = Join-Path $tmpBase 'stage-state-test.json'
$stateBoundary = New-D4InMemoryOs -Root $tmpBase
$out1 = Save-D4StageState -StatePath $statePath -PackageHash $pkg -RunId $runId -Stage 'S1' -Verdict 'pass' -Detail 't' -FileBoundary $stateBoundary.Boundary
Assert-Equal ([string]$out1.stages['S1'].verdict) 'pass' 'S1 verdict recorded'
$tampered = @{ Entries = $manifest['Entries']; ClosureHash = ('e' * 64) }
$tamperedPkg = ('e' * 64)
Assert-Throws { Save-D4StageState -StatePath $statePath -PackageHash $tamperedPkg -RunId $runId -Stage 'S2' -Verdict 'pass' -Detail 't' -FileBoundary $stateBoundary.Boundary } 'a different package hash refuses to reuse old PASS markers' 'DIFFERENT package'
Assert-Throws { Test-D4StageAdvance -StatePath $statePath -PackageHash $tamperedPkg -RunId $runId -Stage 'E1' -FileBoundary $stateBoundary.Boundary } 'advance check refuses a foreign package' 'DIFFERENT package'
$otherRun = $runId + '-x'
Assert-Throws { Read-D4StageState -StatePath $statePath -PackageHash $pkg -RunId $otherRun -FileBoundary $stateBoundary.Boundary } 'reading with the wrong run id refuses' 'not run'
Assert-Throws { Test-D4StageAdvance -StatePath $statePath -PackageHash $pkg -RunId $runId -Stage 'E1' -FileBoundary $stateBoundary.Boundary } 'E1 without S0 refuses (disorder)' 'S0 is not recorded as pass'
# A prior FAILED stage never advances (fresh state: only S0 fail recorded).
$statePath2 = Join-Path $tmpBase 'stage-state-fail.json'
$stateBoundary2 = New-D4InMemoryOs -Root $tmpBase
[void](Save-D4StageState -StatePath $statePath2 -PackageHash $pkg -RunId $runId -Stage 'S0' -Verdict 'fail' -Detail 'probe' -FileBoundary $stateBoundary2.Boundary)
Assert-Throws { Test-D4StageAdvance -StatePath $statePath2 -PackageHash $pkg -RunId $runId -Stage 'S1' -FileBoundary $stateBoundary2.Boundary } 'a prior FAILED stage never advances' 'S0 is not recorded as pass'
Assert-Throws { Save-D4StageState -StatePath $statePath -PackageHash $pkg -RunId $runId -Stage 'S1' -Verdict 'pass' -Detail 't' -FileBoundary $stateBoundary.Boundary } 're-recording the same stage refuses (re-run needs a new run id)' 'already recorded'
$corruptPath = Join-Path $tmpBase 'corrupt-state.json'
$corruptOs = New-D4InMemoryOs -Root $tmpBase
[IO.File]::WriteAllText($corruptPath, '{broken', (New-Object System.Text.UTF8Encoding($false)))
Assert-Throws { Read-D4StageState -StatePath $corruptPath -PackageHash $pkg -RunId $runId -FileBoundary $realBoundary } 'a corrupt stage state refuses' 'corrupt'

# ---- intent ledger (R62-B) ----------------------------------------------------
Write-Host '== intent ledger: persisted before creation, foreign binding refuses =='
$ledgerOs = New-D4InMemoryOs -Root $tmpBase
$ledgerPath = Join-Path $tmpBase 'intent-test.json'
$ledger = New-D4IntentLedger -Inventory $inventory -LedgerPath $ledgerPath -FileBoundary $ledgerOs.Boundary
Assert-Equal @($ledger['entries']).Count (@($inventory['Dirs']).Count + @($inventory['Files'] | Where-Object { $_.Kind -notin 'intent-ledger', 'stage-state' }).Count + @($inventory['Tasks']).Count + @($inventory['RuleIds']).Count) 'ledger plans every non-evidence object'
$ledger2 = Update-D4IntentLedger -Ledger $ledger -LedgerPath $ledgerPath -Path (Join-Path $contract['ExperimentRoot'] 'image') -Status 'created' -FileBoundary $ledgerOs.Boundary
# 'created' entries are the bounded-recovery list (created but not yet
# cleaned); only 'removed'/'failed-clean' resolve an entry.
$unresolved = @(Get-D4LedgerUnresolved -Ledger $ledger2)
Assert-True (($unresolved -join ';') -match [regex]::Escape((Join-Path $contract['ExperimentRoot'] 'image'))) 'a created-but-uncleaned object stays on the bounded-recovery list'
$ledger3 = Update-D4IntentLedger -Ledger $ledger2 -LedgerPath $ledgerPath -Path (Join-Path $contract['ExperimentRoot'] 'image') -Status 'removed' -FileBoundary $ledgerOs.Boundary
$unresolved3 = @(Get-D4LedgerUnresolved -Ledger $ledger3)
Assert-True (($unresolved3 -join ';') -notmatch [regex]::Escape((Join-Path $contract['ExperimentRoot'] 'image'))) 'a removed object is resolved'
$readBack = Read-D4IntentLedger -LedgerPath $ledgerPath -PackageHash $pkg -RunId $runId -FileBoundary $ledgerOs.Boundary
Assert-Equal $readBack['RunId'] $runId 'ledger read-back binds the run id'
Assert-Equal $readBack['PackageHash'] $pkg 'ledger read-back binds the package hash'
Assert-Throws { Read-D4IntentLedger -LedgerPath $ledgerPath -PackageHash $pkg -RunId $otherRun -FileBoundary $ledgerOs.Boundary } 'a foreign run cannot read the ledger' 'not run'
Assert-Equal (Read-D4IntentLedger -LedgerPath (Join-Path $tmpBase 'absent.json') -PackageHash $pkg -RunId $runId -FileBoundary $ledgerOs.Boundary) $null 'an absent ledger is an explicit NULL (nothing was planned)'

# ---- namespace free: three-state (R62-B) --------------------------------------
Write-Host '== namespace: free / occupied / unconfirmed three-state =='
$nsOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$absentTask = New-D4AbsentTaskRunner
$emptyNet = (New-D4EmptyRunner -Tag 'net')
$nsFree = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $absentTask -NetStatRunner $emptyNet.Runner -FileBoundary $nsOs.Boundary
Assert-Equal $nsFree['Verdict'] 'free' 'empty namespace with legal absence + no listener is FREE'
$badNet = {
    param([string[]]$argv)
    return (New-TestRunResult -ExitCode 1 -Output @('netstat failed'))
}.GetNewClosure()
$nsNoNet = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $absentTask -NetStatRunner $null -FileBoundary $nsOs.Boundary
Assert-Equal $nsNoNet['Verdict'] 'unconfirmed' 'a missing netstat collector is UNCONFIRMED (port state never assumed)'
$nsNetFail = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $absentTask -NetStatRunner $badNet -FileBoundary $nsOs.Boundary
Assert-Equal $nsNetFail['Verdict'] 'unconfirmed' 'a netstat query FAILURE is unconfirmed (never absence)'
$occOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$occOs.State['Files'][($contract['ExperimentRoot'] + '\foreign.txt').ToLowerInvariant()] = 'FOREIGN'
$nsOcc = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $absentTask -NetStatRunner $emptyNet.Runner -FileBoundary $occOs.Boundary
Assert-Equal $nsOcc['Verdict'] 'occupied' 'unknown root content OCCUPIES the namespace'
$taskFail = @{
    Runner = {
        param([string[]]$argv)
        return (New-TestRunResult -ExitCode 5 -Output @('ERROR: something else'))
    }.GetNewClosure()
}
$nsTaskFail = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $taskFail.Runner -NetStatRunner $emptyNet.Runner -FileBoundary $nsOs.Boundary
Assert-Equal $nsTaskFail['Verdict'] 'unconfirmed' 'a task query FAILURE is unconfirmed (never absent)'
# The run's OWN evidence record does NOT occupy the namespace (R62-A S0->S1).
$postS0Os = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$postS0Os.State['Dirs'][($contract['ExperimentRoot'] + '\evidence').ToLowerInvariant()] = $true
$statePathFixed = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
$postS0Os.State['Files'][$statePathFixed.ToLowerInvariant()] = '{"closure_hash":"' + $pkg + '","run_id":"' + $runId + '","stages":{}}'
$nsPost = Test-D4NamespaceFree -Inventory $inventory -Contract $contract -SchtasksRunner $absentTask -NetStatRunner $emptyNet.Runner -FileBoundary $postS0Os.Boundary
Assert-Equal $nsPost['Verdict'] 'free' "the run's own evidence record is planned ownership, not foreign takeover"

# ---- task definition documents (R62-D): triggers carried by XML, not /SC -------
Write-Host '== task definitions: legal XML, no /SC enums =='
$cfgOD = New-BrokerConfig -RunRoot $contract['ExperimentRoot'] -SourceRoot $sourceRoot `
    -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
    -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
    -OperatorAccount 'dacint' -PythonHome 'C:\PythonExp' -SharePort 9100 -TaskTrigger 'ONDEMAND'
$defOD = New-BrokerTaskDefinitionXml -Cfg $cfgOD -WrapperPath (Join-Path $contract['ExperimentRoot'] 'run-broker.cmd')
Assert-True ($defOD -match '<Triggers />') 'ONDEMAND definition carries NO trigger elements (on-demand only)'
$defXml = [xml]$defOD
$triggers = @($defXml.Task.Triggers.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
Assert-Equal $triggers.Count 0 'the ONDEMAND definition parses to ZERO trigger elements'
Assert-Equal $defXml.Task.Settings.AllowStartOnDemand 'true' 'AllowStartOnDemand is enabled (a legal manual-start task)'
$cfgST = New-BrokerConfig -RunRoot $contract['ExperimentRoot'] -SourceRoot $sourceRoot `
    -TaskName 'umshare-broker' -ShareTaskName 'umshare-live' `
    -BrokerPipeName '\\.\pipe\um-share-broker' -InstancePipePrefix '\\.\pipe\um-share-instance-' `
    -OperatorAccount 'dacint' -PythonHome 'C:\py' -SharePort 8100
$defST = New-BrokerTaskDefinitionXml -Cfg $cfgST -WrapperPath 'C:\ProgramData\UMShareRun\run-broker.cmd'
$stXml = [xml]$defST
Assert-True ($defST -match '<BootTrigger>') 'production ONSTART definition keeps the boot trigger'
Assert-Equal @($stXml.Task.Triggers.ChildNodes | Where-Object { $_.NodeType -eq 'Element' }).Count 1 'ONSTART carries exactly one boot trigger'
# The definition document is an INSTALL ARTIFACT: registration consumes /Create /XML.
# (Register-BrokerTask reads the wrapper + definition through the REAL
# filesystem - the install lib's boundary - so this section uses a REAL temp
# RunRoot like the install suite; the fake OS sections never call it.)
$regRoot = Join-Path $tmpBase 'reg-root'
New-Item -ItemType Directory -Path (Join-Path $regRoot 'broker') -Force | Out-Null
$cfgReg = New-BrokerConfig -RunRoot $regRoot -SourceRoot $sourceRoot `
    -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
    -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
    -OperatorAccount 'dacint' -PythonHome 'C:\PythonExp' -SharePort 9100 -TaskTrigger 'ONDEMAND'
$defReg = New-BrokerTaskDefinitionXml -Cfg $cfgReg -WrapperPath (Join-Path $regRoot 'run-broker.cmd')
$regDefPath = Join-Path $regRoot 'broker\task-definition.xml'
[IO.File]::WriteAllText($regDefPath, $defReg, (New-Object System.Text.UTF8Encoding($false)))
$regWrapper = Join-Path $regRoot 'run-broker.cmd'
[IO.File]::WriteAllText($regWrapper, (New-BrokerWrapperContent -Cfg $cfgReg), (New-Object System.Text.UTF8Encoding($false)))
$fakeSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Register-BrokerTask -Cfg $cfgReg -WrapperPath $regWrapper -SchtasksRunner $fakeSch.Runner -PrevOwned $false
Assert-True ((@($fakeSch.State['Calls']) | Where-Object { $_ -match '/Create' -and $_ -match '/XML' }).Count -eq 1) 'registration creates the task VIA /Create /XML (no /SC schedule-type enum)'
Assert-True ((@($fakeSch.State['Calls']) | Where-Object { $_ -match '/SC ' }).Count -eq 0) 'NO /SC ONDEMAND enum is ever passed (R62-D)'
Assert-True ($fakeSch.State['Tasks'].ContainsKey($contract['BrokerTaskName'].ToLowerInvariant())) 'the created broker task definition is stored'
# A replaced definition file refuses registration.
[IO.File]::WriteAllText($regDefPath, $defReg.Replace('<Triggers />', '<Triggers><BootTrigger /></Triggers>'), (New-Object System.Text.UTF8Encoding($false)))
$fakeSch2 = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Assert-Throws { Register-BrokerTask -Cfg $cfgReg -WrapperPath $regWrapper -SchtasksRunner $fakeSch2.Runner -PrevOwned $false } 'a replaced definition document refuses registration' 'definition content differs'
[IO.File]::WriteAllText($regDefPath, $defReg, (New-Object System.Text.UTF8Encoding($false)))

# ---- Confirm-D4TaskIdentity: full definition identity (R62-C) ----------------
Write-Host '== task identity: exact definition, never a weak substring =='
function New-OwnedDefinitionXml {
    param([string]$Command, [string]$Principal = 'S-1-5-18', [string]$Arguments = '',
          [string]$TriggerXml = '<Triggers />', [string]$AllowOnDemand = 'true',
          [string]$LogonType = 'S4U', [string]$RunLevel = 'HighestAvailable',
          [bool]$ComHandler = $false, [string]$WorkingDirectory = '')
    $com = ''
    if ($ComHandler) { $com = '<ComHandler><ClassId>{12345678-1234-1234-1234-123456789012}</ClassId></ComHandler>' }
    $wd = ''
    if ($WorkingDirectory) { $wd = '<WorkingDirectory>' + $WorkingDirectory + '</WorkingDirectory>' }
    return ('<?xml version="1.0" encoding="UTF-16"?>' +
            '<Task version="1.2">' + $TriggerXml +
            '<Principals><Principal id="Author"><UserId>' +
            $Principal + '</UserId><LogonType>' + $LogonType + '</LogonType><RunLevel>' + $RunLevel + '</RunLevel></Principal></Principals><Settings><AllowStartOnDemand>' +
            $AllowOnDemand + '</AllowStartOnDemand></Settings><Actions Context="Author"><Exec>' +
            '<Command>' + $Command + '</Command><Arguments>' + $Arguments + '</Arguments>' +
            $wd + '</Exec>' + $com + '</Actions></Task>')
}
$ownedCmd = Join-Path $contract['ExperimentRoot'] 'run-broker.cmd'
function New-DefQueryRunner {
    param([string]$Definition, [string]$Mode = 'owned')
    return {
        param([string[]]$argv)
        if ($Mode -eq 'absent') {
            return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
        }
        if ($Mode -eq 'queryfailed') {
            return (New-TestRunResult -ExitCode 1 -Output @('ERROR: Access is denied.'))
        }
        return (New-TestRunResult -ExitCode 0 -Output @($Definition))
    }.GetNewClosure()
}
$idTask = @{ Name = $contract['BrokerTaskName']; IdentitySid = 'S-1-5-18'; LogonType = 'S4U'; Command = $ownedCmd; Trigger = 'ONDEMAND' }
$idOwned = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd))
Assert-Equal $idOwned.State 'owned' 'the exact definition is OWNED'
Assert-Equal $idOwned.Command $ownedCmd 'the owned definition command is the experiment wrapper'
$idForeign = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command 'C:\evil\other.cmd'))
Assert-Equal $idForeign.State 'mismatch' 'a foreign wrapper is a MISMATCH, never owned'
$idArgs = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -Arguments '-flag'))
Assert-Equal $idArgs.State 'mismatch' 'extra arguments are a MISMATCH'
$idPrincipal = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -Principal 'S-1-5-32-544'))
Assert-Equal $idPrincipal.State 'mismatch' 'a wrong principal is a MISMATCH'
$idCom = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -ComHandler $true))
Assert-Equal $idCom.State 'mismatch' 'a ComHandler sibling is a MISMATCH (not silently ignored)'
$idTrig = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -TriggerXml '<Triggers><BootTrigger /></Triggers>'))
Assert-Equal $idTrig.State 'mismatch' 'an on-demand task with an automatic trigger is a MISMATCH'
$idNoOnDemand = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -AllowOnDemand 'false'))
Assert-Equal $idNoOnDemand.State 'mismatch' 'a definition with AllowStartOnDemand=false is a MISMATCH'
$idWd = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition (New-OwnedDefinitionXml -Command $ownedCmd -WorkingDirectory 'C:\ProgramData\UMShareExp-broker'))
Assert-Equal $idWd.State 'mismatch' 'a WorkingDirectory is a MISMATCH (the wrapper pins its own cwd)'
$idAbsent = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition '' -Mode 'absent')
Assert-Equal $idAbsent.State 'absent' 'the explicit "cannot find" evidence is ABSENT'
$idFail = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner (New-DefQueryRunner -Definition '' -Mode 'queryfailed')
Assert-Equal $idFail.State 'unconfirmed' 'a query FAILURE is unconfirmed, never absent'

# ---- Stop-D4TaskObject: stop/delete ONLY after identity + process exit (R62-C)
Write-Host '== stop: identity-gated, process-exit-confirmed =='
function New-ProcessRunner {
    param([int[]]$Pids, [string]$Mode = 'ok')
    return {
        param([string[]]$argv)
        if ($Mode -eq 'failed') { return (New-TestRunResult -ExitCode 1 -Output @('')) }
        if ($Pids.Count -eq 0) { return (New-TestRunResult -ExitCode 0 -Output @('')) }
        $rows = @()
        foreach ($p in $Pids) {
            # Well-formed JSON (the real Get-CimInstance | ConvertTo-Json
            # escapes backslashes the same way - a raw \P would be an
            # invalid JSON escape and a legitimate Unconfirmed).
            $exe = ($contract['ExperimentRoot'] + '\um-share-python.exe').Replace('\', '\\')
            $rows += ('[{"ProcessId":' + $p + ',"CommandLine":"x","ExecutablePath":"' +
                      $exe + '"}]')
        }
        return (New-TestRunResult -ExitCode 0 -Output @(($rows -join '')))
    }.GetNewClosure()
}
$stopSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4InMemoryOs -Root $contract['ExperimentRoot']).Boundary }
$stopTask = @{ Name = $contract['BrokerTaskName']; IdentitySid = 'S-1-5-18'; LogonType = 'S4U'; Command = $ownedCmd; Root = $contract['ExperimentRoot'] }
$defOwned = New-OwnedDefinitionXml -Command $ownedCmd
$stopSch.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()] = $defOwned
$st1 = Stop-D4TaskObject -Task $stopTask -Trigger 'ONDEMAND' -SchtasksRunner $stopSch.Runner -ProcessEvidenceRunner (New-ProcessRunner -Pids @())
Assert-True ($st1.Stopped -and $st1.Deleted) 'an owned task with no live process is stopped and deleted'
Assert-True ((@($stopSch.State['Calls']) | Where-Object { $_ -match '/End' }).Count -ge 1) '/End was issued after identity verification'
Assert-True ($stopSch.State['Deleted'].ContainsKey($contract['BrokerTaskName'].ToLowerInvariant())) '/Delete confirmed the task removal'
# Absent task -> idempotent, ZERO /End /Delete.
$absentSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4InMemoryOs -Root $contract['ExperimentRoot']).Boundary }
$st2 = Stop-D4TaskObject -Task $stopTask -Trigger 'ONDEMAND' -SchtasksRunner $absentSch.Runner -ProcessEvidenceRunner (New-ProcessRunner -Pids @())
Assert-Equal $st2.Reason 'absent' 'an absent task is an explicit idempotent fact'
Assert-Equal ((@($absentSch.State['Calls']) | Where-Object { $_ -match '/End' -or $_ -match '/Delete' }).Count) 0 'an absent task receives ZERO /End /Delete'
# Foreign wrapper -> refusal with ZERO /End /Delete.
$foreignSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4InMemoryOs -Root $contract['ExperimentRoot']).Boundary }
$foreignSch.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()] = (New-OwnedDefinitionXml -Command 'C:\evil\other.cmd')
Assert-Throws { Stop-D4TaskObject -Task $stopTask -Trigger 'ONDEMAND' -SchtasksRunner $foreignSch.Runner -ProcessEvidenceRunner (New-ProcessRunner -Pids @()) } 'a foreign task definition refuses stop/delete' 'identity NOT confirmed'
Assert-Equal ((@($foreignSch.State['Calls']) | Where-Object { $_ -match '/End' -or $_ -match '/Delete' }).Count) 0 'a foreign task receives ZERO /End /Delete'
# Process still alive after /End -> refuse the /Delete.
$aliveSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4InMemoryOs -Root $contract['ExperimentRoot']).Boundary }
$aliveSch.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()] = $defOwned
Assert-Throws { Stop-D4TaskObject -Task $stopTask -Trigger 'ONDEMAND' -SchtasksRunner $aliveSch.Runner -ProcessEvidenceRunner (New-ProcessRunner -Pids @(4242)) } 'a live process after /End refuses the task deletion' 'still alive'
Assert-Equal ((@($aliveSch.State['Calls']) | Where-Object { $_ -match '/Delete' }).Count) 0 'no /Delete while the process is alive'
# Process evidence query failure -> refuse (unconfirmed is never success).
$failSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4InMemoryOs -Root $contract['ExperimentRoot']).Boundary }
$failSch.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()] = $defOwned
Assert-Throws { Stop-D4TaskObject -Task $stopTask -Trigger 'ONDEMAND' -SchtasksRunner $failSch.Runner -ProcessEvidenceRunner (New-ProcessRunner -Pids @() -Mode 'failed') } 'an unconfirmed process query refuses the deletion' 'unconfirmed'

# ---- E1 (W2) process-evidence script contract: no WQL filter, single quotes ----
# The runner passes the WHOLE -Command script as one string; a WQL -Filter
# (Name='...') and even double-quoted client literals are destroyed by that
# pass-through on this host. Get-D4ProcessEvidence must ship a script whose
# image leaf lives in SINGLE quotes inside a client-side Where-Object, so the
# executed text is exactly the reviewed shape (regression guard).
Write-Host '== process evidence: single-quoted client match + empty is not unconfirmed =='
$peScriptRunner = {
    param([string[]]$ArgumentList)
    # The runner receives (powershell, -NoProfile, -Command, <script>); the
    # product's leaf arrives INSIDE the script text. Verify the script shape
    # AND simulate a real query result.
    $scriptText = [string]$ArgumentList[3]
    $leaf = 'um-share-python.exe'
    $noLeaf = 'no-such-leaf.exe'
    if ($scriptText.Contains("`$_.Name -eq '" + $leaf + "'")) {
        return (New-TestRunResult -ExitCode 0 -Output @(
            '[{"ProcessId":4242,"CommandLine":"C:\\ProgramData\\UMShareExp-broker\\um-share-python.exe x","ExecutablePath":"C:\\ProgramData\\UMShareExp-broker\\um-share-python.exe"}]'))
    }
    if ($scriptText.Contains("`$_.Name -eq '" + $noLeaf + "'")) {
        return (New-TestRunResult -ExitCode 0 -Output @('[]'))
    }
    return (New-TestRunResult -ExitCode 2 -Output @('unexpected script shape: ' + $scriptText))
}.GetNewClosure()
# Single-quoted client match for the real image leaf.
$pe1 = Get-D4ProcessEvidence -ImageLeaf 'um-share-python.exe' -ExperimentRoot $contract['ExperimentRoot'] -Runner $peScriptRunner
Assert-True ($pe1.Unconfirmed -eq $false -and (@($pe1.Pids) -contains 4242)) 'the single-quoted client match returns the real pid (unconfirmed=false)'
# A leaf with no match returns ZERO pids, NOT unconfirmed.
$pe2 = Get-D4ProcessEvidence -ImageLeaf 'no-such-leaf.exe' -ExperimentRoot $contract['ExperimentRoot'] -Runner $peScriptRunner
Assert-True ($pe2.Unconfirmed -eq $false -and @($pe2.Pids).Count -eq 0) 'an absent image leaf is an explicit empty result, never unconfirmed (R62-B three-state)'

# ---- Test-D4CleanupComplete: mandatory evidence (R62-C) -----------------------
Write-Host '== cleanup complete: evidence REQUIRED, never assumed =='
$cleanOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$cleanLedgerPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$cleanLedger = New-D4IntentLedger -Inventory $inventory -LedgerPath $cleanLedgerPath -FileBoundary $cleanOs.Boundary
$cleanLedger = Update-D4IntentLedger -Ledger $cleanLedger -LedgerPath $cleanLedgerPath -Path (Join-Path $contract['ExperimentRoot'] 'image') -Status 'removed' -FileBoundary $cleanOs.Boundary
$emptyRules = New-D4FakeRuleStore -CreatedFacts @{}
$cleanMissing = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-Equal @($cleanMissing).Count 0 'cleanup passes only with collected evidence + resolved owned objects'
$noProc = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence $null -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($noProc -join ';') -match 'process evidence was NOT collected') 'MISSING process evidence can never pass'
$unconfProc = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $true; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($unconfProc -join ';') -match 'query FAILED') 'a FAILED process query is unconfirmed, cleanup cannot pass'
$livePort = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $true } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($livePort -join ';') -match 'still has a listener') 'a live 9100 listener blocks the pass'
$rulesPresent = New-D4FakeRuleStore -CreatedFacts $createdFacts -SeedLive $createdFacts
$ruleLeak = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $rulesPresent.Runner)
Assert-True (($ruleLeak -join ';') -match 'limit rule still present') 'a PRESENT limit rule blocks the pass'
$noRules = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $cleanLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $null)
Assert-True (($noRules -join ';') -match 'no limit-rule collector supplied') 'a MISSING rules collector blocks the pass'
$noLedger = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $null -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $cleanOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($noLedger -join ';') -match 'intent ledger was NOT provided') 'a missing ledger blocks the pass (owned objects unverifiable)'
# KEPT foreign objects are LISTED, not deleted.
$keepOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$keepLedger = @{ run_id = $runId; package_hash = $pkg; entries = @(
    @{ path = (Join-Path $contract['ExperimentRoot'] 'kept.txt'); kind = 'deployment-fact'; status = 'failed-clean' }
) }
$kept = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $keepLedger -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $keepOs.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($kept -join ';') -match 'failed-clean') 'a kept failed-clean object is listed, never claimed cleaned'

# ---- P1: wrapper argv contract ---------------------------------------------
Write-Host '== P1: wrapper argv (owner value, SID/SDDL separate, spaces preserved) =='
$cfgA = New-BrokerConfig -RunRoot $contract['ExperimentRoot'] -SourceRoot $sourceRoot `
    -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
    -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
    -OperatorAccount 'dacint' -InstanceOwnerSid 'S-1-5-18-exp' -BrokerPipeSddl 'D:P(A;;FA;;;SY)' `
    -PythonHome 'C:\Python Exp Home' -SharePort 9100 -ShareImage 'um-share-python.exe' `
    -ExpectedOwnerAccount 'DAICNT\um share' -TaskTrigger 'ONDEMAND'
$contentA = New-BrokerWrapperContent -Cfg $cfgA
$argvA = Get-BrokerWrapperServerArgv -WrapperContent $contentA -TargetRoot $contract['ExperimentRoot']
$ownerIdx = @($argvA).IndexOf('--expected-owner-account')
Assert-True ($ownerIdx -ge 0) 'owner option present in argv'
Assert-Equal @( @($argvA) | Where-Object { $_ -eq '--expected-owner-account' } ).Count 1 'owner option appears EXACTLY once (no duplicate embedding)'
Assert-Equal $argvA[$ownerIdx + 1] 'DAICNT\um share' 'owner account value is ONE token (spaces preserved)'
$sidIdx = @($argvA).IndexOf('--expect-owner-sid')
Assert-Equal $argvA[$sidIdx + 1] 'S-1-5-18-exp' 'owner SID is its own option and value'
$sddlIdx = @($argvA).IndexOf('--sddl')
Assert-Equal $argvA[$sddlIdx + 1] 'D:P(A;;FA;;;SY)' 'pipe SDDL is its own option and value'
Assert-Equal (@($argvA) | Where-Object { $_ -match 'expected-owner-account' }).Count 1 'no re-nested option fragment survives'
$cfgSp = New-BrokerConfig -RunRoot 'C:\Program Files\UMShareExp' -SourceRoot $sourceRoot `
    -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
    -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
    -OperatorAccount 'dacint' -PythonHome 'C:\py' -SharePort 9100
$argvSp = Get-BrokerWrapperServerArgv -WrapperContent (New-BrokerWrapperContent -Cfg $cfgSp) -TargetRoot 'C:\Program Files\UMShareExp'
Assert-Equal @( @($argvSp) | Where-Object { $_ -eq 'C:\Program Files\UMShareExp\broker' } ).Count 1 'spaced target root keeps --broker-dir value as ONE token'
Assert-True (-not (@($argvA) -contains '%*')) 'operator tail %* is not part of the install argv'
$cfgB = New-BrokerConfig -RunRoot $contract['ExperimentRoot'] -SourceRoot $sourceRoot `
    -TaskName $contract['BrokerTaskName'] -ShareTaskName $contract['ShareTaskName'] `
    -BrokerPipeName $contract['BrokerPipeName'] -InstancePipePrefix $contract['InstancePipePrefix'] `
    -OperatorAccount 'dacint' -PythonHome 'C:\py' -SharePort 9100
$argvB = Get-BrokerWrapperServerArgv -WrapperContent (New-BrokerWrapperContent -Cfg $cfgB) -TargetRoot $contract['ExperimentRoot']
$ownerIdxB = @($argvB).IndexOf('--expected-owner-account')
Assert-Equal $argvB[$ownerIdxB + 1] '' 'empty account stays an explicit empty value (fail-closed at the server)'

# ---- P3: prerequisites (deployment-fact contract) + trigger -----------------
Write-Host '== P3: install prerequisites use the deployment-mode fact =='
function New-PrereqRoot {
    param([string]$Dir, [string]$ModeJson)
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $Dir 'um-share-python.exe') -Value 'dummy'
    if ($ModeJson) {
        Set-Content -LiteralPath (Join-Path $Dir 'deploy-mode.json') -Value $ModeJson
    }
    return $Dir
}
$cfgPr = New-BrokerConfig -RunRoot (Join-Path $tmpBase 'pr') -SourceRoot $sourceRoot `
    -TaskName 't' -ShareTaskName 'st' -BrokerPipeName '\\.\pipe\p' -InstancePipePrefix '\\.\pipe\i-' `
    -OperatorAccount 'dacint' -PythonHome 'C:\py' -SharePort 9100
$prRoot = New-PrereqRoot -Dir $cfgPr['RunRoot'] -ModeJson $null
Assert-Throws { Test-Prerequisites -Cfg $cfgPr } 'missing deploy-mode.json refuses (the old hand-written guard-status bypass is gone)' 'deployment mode file missing'
$prRoot = New-PrereqRoot -Dir $cfgPr['RunRoot'] -ModeJson '{ broken'
Assert-Throws { Test-Prerequisites -Cfg $cfgPr } 'corrupt mode file refuses' 'corrupt'
$prRoot = New-PrereqRoot -Dir $cfgPr['RunRoot'] -ModeJson '{"mode": "garbage"}'
Assert-Throws { Test-Prerequisites -Cfg $cfgPr } 'unknown mode refuses' "must be 'projection' or 'legacy'"
$prRoot = New-PrereqRoot -Dir $cfgPr['RunRoot'] -ModeJson '{"mode": "projection"}'
Assert-Throws { Test-Prerequisites -Cfg $cfgPr } 'projection mode without the derived projection refuses' 'derived projection is missing'
Set-Content -LiteralPath (Join-Path $cfgPr['RunRoot'] 'um-share-projection.json') -Value '{"schema_version": 1, "config_generation": 1, "data": []}'
Test-Prerequisites -Cfg $cfgPr
Assert-True $true 'projection mode with the derived file passes'
$prRoot = New-PrereqRoot -Dir $cfgPr['RunRoot'] -ModeJson '{"mode": "legacy"}'
Test-Prerequisites -Cfg $cfgPr
Assert-True $true 'legacy mode without a projection passes'
Remove-Item -LiteralPath (Join-Path $cfgPr['RunRoot'] 'deploy-mode.json') -Force
Set-Content -LiteralPath (Join-Path $cfgPr['RunRoot'] 'guard-status.json') -Value '{"guard": "ok"}'
Assert-Throws { Test-Prerequisites -Cfg $cfgPr } 'a hand-written guard-status.json can NOT bypass the gate' 'deployment mode file missing'

# ---- P2: share launch contract (PS mirror) ---------------------------------
Write-Host '== P2: share launch args + capture script (two pipes, two DACLs) =='
$shareArgs = New-D4ShareLaunchArgs -Contract $contract `
    -InstanceKeyPath (Join-Path $contract['ExperimentRoot'] 'instance-channel-key') `
    -RegistrationPath (Join-Path $contract['ExperimentRoot'] 'run\live\instance-registration.json') `
    -InstancePipeSddl 'D:P(A;;FR;;;S-1-5-18-exp)'
Assert-True ($shareArgs.Contains('--instance-pipe-sddl "D:P(A;;FR;;;S-1-5-18-exp)"')) 'instance SDDL is explicitly carried to the share child'
Assert-True ($shareArgs.Contains('--instance-pipe-prefix "\\.\pipe\um-share-instance-exp-"')) 'instance pipe prefix is the experiment prefix'
$tamperedContract = @{ InstancePipePrefix = $contract['ProductionInstancePrefix'] }
Assert-Throws { New-D4ShareLaunchArgs -Contract $tamperedContract -InstanceKeyPath 'k' -RegistrationPath 'r' -InstancePipeSddl 'D:P' } 'production instance prefix refuses the share launch' 'experiment prefix'
Assert-Throws { New-D4ShareLaunchArgs -Contract $contract -InstanceKeyPath 'k' -RegistrationPath 'r' -InstancePipeSddl '' } 'empty instance SDDL refuses' 'SDDL is required'
Assert-Throws { New-D4ShareLaunchArgs -Contract $contract -InstanceKeyPath '' -RegistrationPath 'r' -InstancePipeSddl 'D:P' } 'missing instance key path refuses' 'instance key path is required'
$capture = New-D4ShareCaptureScriptContent -Contract $contract -ShareArgs $shareArgs -PythonHome 'C:\PythonExp'
Assert-True ($capture -match 'UM_SHARE_ARGS=') 'capture script passes the composed args via UM_SHARE_ARGS'
Assert-True ($capture -match '--port 9100 --probe') 'capture script pins probe + port 9100'
Assert-True ($capture -match '--experiment-config') 'capture script pins the experiment config (guard launch-pinning consumer)'
Assert-True ($capture -match [regex]::Escape('--run-dir "C:\ProgramData\UMShareExp-broker\run\live"')) 'capture script pins run-dir by EXPRESSION-mode join (PS 5.1 argument-mode cast trap regression)'
Assert-True ($capture -match 'cd /d "%~dp0"') 'capture script pins its own working directory (task cwd never assumed)'
Assert-True ($capture -match [regex]::Escape('"C:\ProgramData\UMShareExp-broker\um-share-python.exe" "C:\ProgramData\UMShareExp-broker\share_guard.py"')) 'capture script launches the root-LOCAL image + guard by ABSOLUTE path'
Assert-Throws { New-D4ShareCaptureScriptContent -Contract $contract -ShareArgs $shareArgs -PythonHome '' } 'capture script refuses to guess the Python home' 'explicit -PythonHome required'

# ---- ENTRY R62-A: three-call S0 -> S1 -> repeat-refusal (same state file) -----
Write-Host '== ENTRY R62-A: S0 -> S1 -> repeat refusal through the REAL entry =='
$os = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$entrySch = New-D4FakeSchtasks -Oses $os
$net = (New-D4EmptyRunner -Tag 'net')
$proc = (New-D4EmptyRunner -Tag 'proc')
$rules = New-D4FakeRuleStore -CreatedFacts $createdFacts
$entryCfgPath = Join-Path $tmpBase 'entry-config.json'
$entryConfig = New-D4TestConfig -RunId $runId -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha })
[IO.File]::WriteAllText($entryCfgPath, ($entryConfig | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$installerCalls = New-Object System.Collections.Generic.List[string]
$brokerInstaller = {
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [scriptblock]$IcaclsRunner, [hashtable]$Boundary)
    $installerCalls.Add('install:' + $Cfg['RunRoot'])
    # R78-C: the injected hook honors the REAL install contract - the
    # manifest (broker\install-state.json) is persisted with created
    # entries carrying the ACTUAL content hashes (the record the S1 import
    # consumes); a pre-existing artifact is 'shared' and stays OUT of the
    # run's intent ledger (the manifest owns its recovery).
    Invoke-D4File -Boundary $Boundary -Operation 'MakeDir' -Arguments @(
        (Join-Path $Cfg['RunRoot'] 'broker'))
    $mods = @()
    foreach ($rel in @('broker\hook-fixture.py', 'broker\broker-control-key',
                       'instance-channel-key', 'run-broker.cmd',
                       'broker\task-definition.xml')) {
        $dst = Join-Path $Cfg['RunRoot'] $rel
        if ($rel -eq 'broker\task-definition.xml') {
            # Faithful to the REAL install: the definition document is the
            # reviewed generated XML (SYSTEM/S4U/ONDEMAND) and the broker
            # task is REGISTERED (never started) - the E1 pre-start identity
            # verification and the cleanup stop/delete consume it.
            $brokerHookXml = New-BrokerTaskDefinitionXml -Cfg $Cfg -WrapperPath (Join-Path $Cfg['RunRoot'] 'run-broker.cmd')
            Invoke-D4File -Boundary $Boundary -Operation 'WriteXml' -Arguments @($dst, $brokerHookXml)
        } else {
            Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
                $dst, ('HOOK-FIXTURE ' + $rel))
        }
        $mods += @{ file = $rel;
                    sha256 = (Invoke-D4File -Boundary $Boundary -Operation 'Hash' -Arguments @($dst));
                    classification = 'created' }
    }
    $mods += @{ file = 'broker\pre-existing.dll'; sha256 = ''; classification = 'shared' }
    Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
        (Join-Path (Join-Path $Cfg['RunRoot'] 'broker') 'install-state.json'),
        (@{ task_name = $Cfg['TaskName']; modules = $mods } | ConvertTo-Json -Depth 4))
    # Register the broker task exactly like the real install does
    # (ONDEMAND, definition-document based, NEVER started).
    [void](Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Create', '/TN', $Cfg['TaskName'], '/XML',
        (Join-Path (Join-Path $Cfg['RunRoot'] 'broker') 'task-definition.xml')))
}.GetNewClosure()
$storeFactory = { param([string]$Path) throw 'store factory must never run offline' }.GetNewClosure()

function New-D4EntrySplat {
    # HASHTABLE parameter splat for the entry (array splats bind positionally
    # and would feed string values into scriptblock slots - never correct).
    param([string]$RunId, [string]$ConfigPath, [scriptblock]$Schtasks,
          [scriptblock]$Net, [scriptblock]$Proc, [scriptblock]$Rules,
          [hashtable]$Os, [scriptblock]$Installer, [scriptblock]$Store,
          [scriptblock]$Acl, [scriptblock]$Probe,
          [switch]$WithoutNet, [switch]$WithoutRules)
    $h = @{
        ExecuteExperiment = $true
        TestHarness = $true
        ApprovedRunId = $RunId
        ConfigPath = $ConfigPath
        SchtasksRunner = $Schtasks
        ProcessEvidenceRunner = $Proc
        FileBoundary = $Os.Boundary
        BrokerInstaller = $Installer
        StoreFactory = $Store
        IcaclsRunner = $Acl
        ProbeRunner = $Probe
        # E1 broker readiness wait: the offline E1 must see the management
        # pipe as ALREADY existing (the entry only checks presence), so the
        # matrix runs without a real broker.
        PipeEvidenceRunner = { return @('\\?\pipe\um-share-broker-exp', '\\.\pipe\um-share-broker-exp') }.GetNewClosure()
    }
    if (-not $WithoutNet) { $h['NetStatRunner'] = $Net }
    if (-not $WithoutRules) { $h['RulesRunner'] = $Rules }
    return $h
}
$aclMain = New-D4HarnessAclRunner -Oses $os
$splat = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $entrySch.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules.Runner -Os $os -Acl $aclMain.Runner -Installer $brokerInstaller -Store $storeFactory

# Call 1: S0.
& $entry -Stage 'S0' @splat | Out-Null
Assert-Equal $LASTEXITCODE 0 'S0 exits 0'
$s0StatePath = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
Assert-True ($os.State['Files'].ContainsKey($s0StatePath.ToLowerInvariant())) 'S0 wrote the evidence record at the package-bound state path'
# Call 2: S1 (advances over the SAME state file, creates the objects).
& $entry -Stage 'S1' @splat | Out-Null
Assert-Equal $LASTEXITCODE 0 'S1 exits 0'
Assert-True ($os.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\run-live.cmd').ToLowerInvariant())) 'S1 created the capture wrapper'
Assert-True ($os.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'S1 created the deployment-mode fact'
Assert-True ($os.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\experiment-config.json').ToLowerInvariant())) 'S1 created the experiment config fact'
Assert-Equal $installerCalls.Count 1 'the reviewed broker-install hook ran exactly once (nothing started)'
Assert-True ((@($entrySch.State['Calls']) | Where-Object { $_ -match '/Create' -and $_ -match 'umshare-share-exp' -and $_ -match '/XML' }).Count -eq 1) 'the share task is registered VIA /XML with NO /SC schedule-type enum'
Assert-True ((@($entrySch.State['Calls']) | Where-Object { $_ -match '/Run' }).Count -eq 0) 'S1 NEVER starts any task'
# Call 3: repeated S1 -> refusal + zero later actions.
$beforeWrites = @($os.State['Writes']).Count
$beforeCreate = @($entrySch.State['Calls'] | Where-Object { $_ -match '/Create' }).Count
Assert-Throws { & $entry -Stage 'S1' @splat } 'a repeated S1 refuses (the stage is already recorded)' 'already recorded'
Assert-True ((@($os.State['Writes']).Count) -eq $beforeWrites) 'the refused repeat wrote ZERO new objects'
Assert-True ((@($entrySch.State['Calls'] | Where-Object { $_ -match '/Create' }).Count) -eq $beforeCreate) 'the refused repeat took ZERO later actions'

# ---- ENTRY R62-A: wrong run id / foreign S0 record occupies the namespace ------
Write-Host '== ENTRY R62-A: a foreign run record across runs =='
$os2 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch2 = New-D4FakeSchtasks -Oses $os2
$cfgPath2A = Join-Path $tmpBase 'entry-config-a.json'
$runIdB = '20260909T130000-abcdef98'
$cfgA2 = New-D4TestConfig -RunId $runId -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha })
[IO.File]::WriteAllText($cfgPath2A, ($cfgA2 | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$pkg2 = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson ([string]$cfgA2['runtime_source_dir'] + '|' + [string]$cfgA2['python_home'] + '|' + 'python-core.bin=' + $rtSha)
$acl2 = New-D4HarnessAclRunner -Oses $os2
$splat2a = New-D4EntrySplat -RunId $runId -ConfigPath $cfgPath2A -Schtasks $sch2.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules.Runner -Os $os2 -Acl $acl2.Runner -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splat2a | Out-Null
$stateA = Get-D4StageStatePath -Contract $contract -PackageHash $pkg2 -RunId $runId
Assert-True ($os2.State['Files'].ContainsKey($stateA.ToLowerInvariant())) 'run A recorded its S0 pass'
$cfgPath2B = Join-Path $tmpBase 'entry-config-b.json'
$cfgB2 = New-D4TestConfig -RunId $runIdB -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha })
[IO.File]::WriteAllText($cfgPath2B, ($cfgB2 | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$sch2b = New-D4FakeSchtasks -Oses $os2
$splat2b = New-D4EntrySplat -RunId $runIdB -ConfigPath $cfgPath2B -Schtasks $sch2b.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules.Runner -Os $os2 -Acl $acl2.Runner -Installer $brokerInstaller -Store $storeFactory
$writesBefore = @($os2.State['Writes']).Count
Assert-Throws { & $entry -Stage 'S0' @splat2b } 'a different run id sees run As record as foreign occupancy' 'S0 inventory verdict=occupied'
Assert-Equal (@($os2.State['Writes']).Count) $writesBefore 'the refused S0 wrote ZERO records'

# ---- ENTRY R62-B: S0 occupied namespace -> zero writes ------------------------
Write-Host '== ENTRY R62-B: S0 refusal writes zero records =='
$os3 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$os3.State['Files'][($contract['ExperimentRoot'] + '\foreign-takeover.txt').ToLowerInvariant()] = 'FOREIGN BYTES'
$sch3 = New-D4FakeSchtasks -Oses $os3
$acl3 = New-D4HarnessAclRunner -Oses $os3
$rules3 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat3 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch3.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules3.Runner -Os $os3 -Acl $acl3.Runner -Installer $brokerInstaller -Store $storeFactory
$writes3 = @($os3.State['Writes']).Count
Assert-Throws { & $entry -Stage 'S0' @splat3 } 'S0 with an occupied namespace refuses before any write' 'S0 inventory verdict=occupied'
Assert-Equal (@($os3.State['Writes']).Count) $writes3 'the refused S0 performed ZERO experiment writes'
$stateS0 = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
Assert-True (-not $os3.State['Files'].ContainsKey($stateS0.ToLowerInvariant())) 'no evidence record exists after the refused S0'

# ---- ENTRY R62-B: foreign file after S0 -> S1 refuses with bytes intact -------
Write-Host '== ENTRY R62-B: S1 adopts nothing, unknown bytes stay intact =='
$os4 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch4 = New-D4FakeSchtasks -Oses $os4
$acl4 = New-D4HarnessAclRunner -Oses $os4
$rules4 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat4 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch4.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules4.Runner -Os $os4 -Acl $acl4.Runner -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splat4 | Out-Null
$foreignPath = $contract['ExperimentRoot'] + '\appeared-after-s0.txt'
$os4.State['Files'][$foreignPath.ToLowerInvariant()] = 'UNTOUCHABLE'
$writes4 = @($os4.State['Writes']).Count
Assert-Throws { & $entry -Stage 'S1' @splat4 } 'S1 refuses when a foreign file appears after S0' 'no longer free'
Assert-Equal (@($os4.State['Writes']).Count) $writes4 'S1 performed ZERO writes before the refusal (ledger not written)'
Assert-Equal $os4.State['Files'][$foreignPath.ToLowerInvariant()] 'UNTOUCHABLE' 'the foreign bytes are unchanged (never taken over)'

# ---- ENTRY R62-A: a changed package invalidates old PASS -----------------------
Write-Host '== ENTRY R62-A: package change invalidates old PASS =='
$os5 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch5 = New-D4FakeSchtasks -Oses $os5
$acl5 = New-D4HarnessAclRunner -Oses $os5
$rules5 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat5 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch5.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules5.Runner -Os $os5 -Acl $acl5.Runner -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splat5 | Out-Null
# A second config with a DIFFERENT python_home = a different package.
$cfgPath5 = Join-Path $tmpBase 'entry-config-5.json'
$cfg5 = New-D4TestConfig -RunId $runId -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$cfg5['python_home'] = 'C:\PythonExp2'
[IO.File]::WriteAllText($cfgPath5, ($cfg5 | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$splat5b = New-D4EntrySplat -RunId $runId -ConfigPath $cfgPath5 -Schtasks $sch5.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules5.Runner -Os $os5 -Acl $acl5.Runner -Installer $brokerInstaller -Store $storeFactory
Assert-Throws { & $entry -Stage 'S1' @splat5b } 'a changed package has no S0 evidence: S1 refuses' 'requires prior stage evidence'

# ---- ENTRY R62-C: cleanup negatives (mandatory evidence, identity-gated) -------
Write-Host '== ENTRY R62-C: cleanup refuses without mandatory evidence =='
$os6 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch6 = New-D4FakeSchtasks -Oses $os6
$acl6 = New-D4HarnessAclRunner -Oses $os6
$rules6 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat6a = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch6.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules6.Runner -Os $os6 -Acl $acl6.Runner -Installer $brokerInstaller -Store $storeFactory `
    -WithoutNet
Assert-Throws { & $entry -Stage 'Cleanup' @splat6a } 'cleanup without a netstat collector refuses' 'netstat collector'
$splat6b = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch6.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules6.Runner -Os $os6 -Acl $acl6.Runner -Installer $brokerInstaller -Store $storeFactory `
    -WithoutRules
Assert-Throws { & $entry -Stage 'Cleanup' @splat6b } 'cleanup without a rules collector refuses' 'limit-rule collector'
# Cleanup over an empty namespace (task absent, no ledger, empty evidence):
# the missing ledger is unconfirmed and the pass is refused; the absent task
# gets ZERO /End /Delete.
$os7 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch7 = New-D4FakeSchtasks -Oses $os7
$acl7 = New-D4HarnessAclRunner -Oses $os7
$rules7 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat7 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch7.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rules7.Runner -Os $os7 -Acl $acl7.Runner -Installer $brokerInstaller -Store $storeFactory
Assert-Throws { & $entry -Stage 'Cleanup' @splat7 } 'cleanup with no ledger cannot pass (owned objects unverifiable)' 'cleanup incomplete'
Assert-Equal ((@($sch7.State['Calls']) | Where-Object { $_ -match '/End' -or $_ -match '/Delete' }).Count) 0 'an absent task receives ZERO /End /Delete during cleanup'

# ---- ENTRY R62-E: limits not ready => ZERO starts; failure never advances ----
Write-Host '== ENTRY R62-E: limits gate refuses before ANY start =='
$osE = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schE = New-D4FakeSchtasks -Oses $osE
$aclE = New-D4HarnessAclRunner -Oses $osE
$rulesE = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splatE = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schE.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $rulesE.Runner -Os $osE -Acl $aclE.Runner -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatE | Out-Null
& $entry -Stage 'S1' @splatE | Out-Null
$failingRules = @{
    Runner = {
        param([string[]]$argv)
        return (New-TestRunResult -ExitCode 2 -Output @('FwpmEngineOpen0 failed'))
    }.GetNewClosure()
}
$splatE2 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schE.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $failingRules.Runner -Os $osE -Acl $aclE.Runner -Installer $brokerInstaller -Store $storeFactory
$runsBefore = @($schE.State['Calls'] | Where-Object { $_ -match '/Run' }).Count
Assert-Throws { & $entry -Stage 'E1' @splatE2 } 'E1 refuses when the limit rules are not in place' 'experiment limits not ready'
Assert-Equal ((@($schE.State['Calls'] | Where-Object { $_ -match '/Run' }).Count)) $runsBefore 'E1 with limits not ready performs ZERO starts'
# The failed E1 is recorded (failure never advances to the next stage).
Assert-Throws { & $entry -Stage 'E2' @splatE2 } 'E2 refuses because E1 did not pass' 'is not recorded as pass'

# ---- E4 PS fault scenarios: wrapper verdict contracts (R62-E) --------------------
Write-Host '== E4 fault scenarios: verdict contract of the three PS boundaries =='
function New-D4ScenarioSourceDir {
    # Temp copy of the REAL reviewed closure (read-only use of repo files).
    param([string]$Dir)
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    foreach ($m in (Get-BrokerSourceManifest -SourceRoot $sourceRoot)) {
        Copy-Item -LiteralPath $m.Src -Destination (Join-Path $Dir (Split-Path $m.Src -Leaf)) -Force
    }
}
function New-D4ScenarioRunRoot {
    param([string]$Dir)
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $Dir 'um-share-python.exe') -Value 'dummy'
    Set-Content -LiteralPath (Join-Path $Dir 'deploy-mode.json') -Value '{"mode": "legacy"}'
}
function New-D4ScenarioCfg {
    param([string]$RunRoot, [string]$SourceRoot)
    return (New-BrokerConfig -RunRoot $RunRoot -SourceRoot $SourceRoot `
        -TaskName "umshare-broker-$(Get-Random)" -ShareTaskName "umshare-live-$(Get-Random)" `
        -BrokerPipeName '\\.\pipe\um-share-broker-scen' `
        -InstancePipePrefix '\\.\pipe\um-share-instance-scen-' `
        -OperatorAccount 'dacint' -InstanceOwnerSid '' -BrokerPipeSddl '' `
        -PythonHome 'C:\PythonTest' -SharePort 9100)
}
function New-D4ScenarioCfgForEntry {
    # R80 E4 reentry base: the ENTRY's E4 builds its OWN New-BrokerConfig
    # from the contract + config (source root, python home, identities, pipe
    # names, task names ...). The scenario-root setup install MUST use the
    # IDENTICAL values - otherwise the reentry sees the pre-existing wrapper
    # / task definition as foreign content and refuses (an E4 that cannot
    # reenter its own install is not the lifecycle being tested). This
    # mirrors execute-d4-experiment.ps1's E4 New-BrokerConfig exactly.
    param([string]$RunRoot, [hashtable]$Contract, [hashtable]$Config)
    return (New-BrokerConfig -RunRoot $RunRoot -SourceRoot $Contract['SourceRootDefault'] `
        -TaskName $Contract['BrokerTaskName'] -ShareTaskName $Contract['ShareTaskName'] `
        -BrokerPipeName $Contract['BrokerPipeName'] -InstancePipePrefix $Contract['InstancePipePrefix'] `
        -OperatorAccount ([string]$Config['operator_account']) -InstanceOwnerSid ([string]$Config['share_sid']) `
        -BrokerPipeSddl ([string]$Config['broker_pipe_sddl']) -PythonHome ([string]$Config['python_home']) `
        -SharePort $Contract['SharePort'] -ShareImage $Contract['ShareImage'] `
        -ExpectedOwnerAccount ([string]$Config['share_account']) `
        -TaskTrigger $Contract['TaskTrigger'])
}
function New-D4GoodIcacls {
    # Canned broker-dir ACL verdict: SYSTEM/admin full, operator RX-only.
    # /save REALLY writes the isolated backup (content = canned ACL) and
    # /restore consumes it - the recovery state machine runs for real.
    return {
        param([string[]]$argv)
        if ($argv -contains '/save') {
            [IO.File]::WriteAllLines($argv[-1], @('SIMULATED-ACL:',
                'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                'BUILTIN\Administrators:(OI)(CI)(F)',
                'MACHINE\dacint:(OI)(CI)(RX)'))
            return (New-TestRunResult -ExitCode 0 -Output @('saved'))
        }
        if ($argv -contains '/restore') {
            if (-not (Test-Path -LiteralPath $argv[-1] -PathType Leaf)) {
                return (New-TestRunResult -ExitCode 1 -Output @('backup missing'))
            }
            Remove-Item -LiteralPath $argv[-1] -Force
            return (New-TestRunResult -ExitCode 0 -Output @('restored'))
        }
        $out = @(
            $argv[1],
            'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
            'BUILTIN\Administrators:(OI)(CI)(F)',
            'MACHINE\dacint:(OI)(CI)(RX)'
        )
        New-TestRunResult -ExitCode 0 -Output $out
    }.GetNewClosure()
}
function New-D4FailingSetIcacls {
    # The E4 ACL-change fault: /save succeeds (backup written) but the ACL
    # SET fails - the rollback (/restore) is delegated to good, so this
    # matches the REAL entry's one-shot /grant fault (the refused install
    # ends in legal state none: rolled back).
    $good = New-D4GoodIcacls
    return {
        param([string[]]$argv)
        $joined = ($argv -join ' ')
        if ($joined -match '/grant:r') {
            return (New-TestRunResult -ExitCode 1 -Output @('INJECTED FAULT: acl change boundary'))
        }
        return (& $good $argv)
    }.GetNewClosure()
}
function New-D4FailingSetAndRestoreIcacls {
    # The blocked-rollback variant: /grant AND /restore fail, so the refused
    # install ends in legal state failed with the backup KEPT for operator
    # review; the reentry's good runner consumes it.
    $good = New-D4GoodIcacls
    return {
        param([string[]]$argv)
        $joined = ($argv -join ' ')
        if ($joined -match '/grant:r' -or $joined -match '/restore') {
            return (New-TestRunResult -ExitCode 1 -Output @('INJECTED FAULT: acl boundary'))
        }
        return (& $good $argv)
    }.GetNewClosure()
}
$scenSrcDir = Join-Path $tmpBase 'scen-src'
New-D4ScenarioSourceDir -Dir $scenSrcDir
# Fresh-root precondition: the ACL-change boundary RE-ENTERS over the
# experiment's own S1 install - a fresh root has no recovery material and
# must be refused with the precondition error.
$scenRootFresh = Join-Path $tmpBase 'scen-root-fresh'
New-D4ScenarioRunRoot -Dir $scenRootFresh
$scenCfgFresh = New-D4ScenarioCfg -RunRoot $scenRootFresh -SourceRoot $scenSrcDir
$scenSchFresh = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Assert-Throws { Invoke-D4FaultScenarioAclChange -RunRoot $scenRootFresh -SourceRoot $scenSrcDir -Cfg $scenCfgFresh `
    -FailingIcacls (New-D4FailingSetIcacls) -GoodIcacls (New-D4GoodIcacls) -SchtasksRunner $scenSchFresh.Runner } 'acl-change on a FRESH root refuses with the precondition' "requires the experiment's own install"
# Branch (a): the real-mode /grant-only fault -> refused + legal state none.
$scenRoot = Join-Path $tmpBase 'scen-root1'
New-D4ScenarioRunRoot -Dir $scenRoot
$scenCfg = New-D4ScenarioCfg -RunRoot $scenRoot -SourceRoot $scenSrcDir
$scenSch = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Install-Broker -Cfg $scenCfg -SchtasksRunner $scenSch.Runner -IcaclsRunner (New-D4GoodIcacls)
$a = @(Invoke-D4FaultScenarioAclChange -RunRoot $scenRoot -SourceRoot $scenSrcDir -Cfg $scenCfg `
    -FailingIcacls (New-D4FailingSetIcacls) -GoodIcacls (New-D4GoodIcacls) -SchtasksRunner $scenSch.Runner)
Assert-Equal ($a -join ',') 'refused,recovery-recorded,reentry-recovery-consumed' 'acl-change scenario (rolled-back) returns the full verdict sequence'
Assert-True (-not (Test-Path -LiteralPath (Get-ManagedPath -TargetRoot $scenRoot -RelPath 'broker-pre-acl.dat'))) 'acl-change reentry leaves no backup file'
$stateS1 = Get-Content -LiteralPath (Join-Path (Join-Path $scenRoot 'broker') 'install-state.json') -Raw | ConvertFrom-Json
Assert-Equal ([string]$stateS1.acl_recovery.state) 'none' 'the rolled-back failure is recorded as legal state none'
# Branch (b): blocked rollback -> refused + legal state failed + kept backup consumed on reentry.
$scenRootB = Join-Path $tmpBase 'scen-root1b'
New-D4ScenarioRunRoot -Dir $scenRootB
$scenCfgB = New-D4ScenarioCfg -RunRoot $scenRootB -SourceRoot $scenSrcDir
$scenSchB = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Install-Broker -Cfg $scenCfgB -SchtasksRunner $scenSchB.Runner -IcaclsRunner (New-D4GoodIcacls)
$b = @(Invoke-D4FaultScenarioAclChange -RunRoot $scenRootB -SourceRoot $scenSrcDir -Cfg $scenCfgB `
    -FailingIcacls (New-D4FailingSetAndRestoreIcacls) -GoodIcacls (New-D4GoodIcacls) -SchtasksRunner $scenSchB.Runner)
Assert-Equal ($b -join ',') 'refused,recovery-recorded,reentry-recovery-consumed' 'acl-change scenario (kept backup) returns the full verdict sequence'
Assert-True (-not (Test-Path -LiteralPath (Get-ManagedPath -TargetRoot $scenRootB -RelPath 'broker-pre-acl.dat'))) 'acl-change kept-material branch consumed the backup on reentry'
$scenRoot2 = Join-Path $tmpBase 'scen-root2'
New-D4ScenarioRunRoot -Dir $scenRoot2
$scenCfg2 = New-D4ScenarioCfg -RunRoot $scenRoot2 -SourceRoot $scenSrcDir
$scenSch2 = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
$c = @(Invoke-D4FaultScenarioBackupCleanup -RunRoot $scenRoot2 -SourceRoot $scenSrcDir -Cfg $scenCfg2 `
    -GoodIcacls (New-D4GoodIcacls) -SchtasksRunner $scenSch2.Runner)
Assert-Equal ($c -join ',') 'recovery-recorded,gc-collected' 'backup-cleanup scenario GCs the state=none survivor'
$scenRoot3 = Join-Path $tmpBase 'scen-root3'
New-D4ScenarioRunRoot -Dir $scenRoot3
$scenCfg3 = New-D4ScenarioCfg -RunRoot $scenRoot3 -SourceRoot $scenSrcDir
$scenSch3 = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
$scenSch3.State['CreateOk'] = $false
$scenSch3b = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
$d = @(Invoke-D4FaultScenarioTaskRegister -RunRoot $scenRoot3 -SourceRoot $scenSrcDir -Cfg $scenCfg3 `
    -GoodIcacls (New-D4GoodIcacls) -FailingSchtasks $scenSch3.Runner -GoodSchtasks $scenSch3b.Runner)
Assert-Equal ($d -join ',') 'refused,recovery-recorded,reentry-completed' 'task-register scenario refuses then completes on reentry'

# ---- R67 acceptance: rules/ACL strong-validation + cleanup closure -------------
Write-Host '== R67-A: rule spec validation (lib, exact specs not bare GUIDs) =='
$badSpecs = @(
    @{ r = 'bad id'; s = @{ id = 'x'; layer = 'connect-v4'; action = 'permit'; app_path = 'p'; remote_addr = '127.0.0.1'; remote_port = 9100 } },
    @{ r = 'permit without scope'; s = @{ id = '9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55'; layer = 'connect-v4'; action = 'permit'; app_path = 'p' } },
    @{ r = 'permit wrong port'; s = @{ id = '9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55'; layer = 'connect-v4'; action = 'permit'; app_path = 'p'; remote_addr = '127.0.0.1'; remote_port = 8000 } },
    @{ r = 'permit wrong addr'; s = @{ id = '9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55'; layer = 'connect-v4'; action = 'permit'; app_path = 'p'; remote_addr = '0.0.0.0'; remote_port = 9100 } },
    @{ r = 'block with scope'; s = @{ id = 'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66'; layer = 'connect-v4'; action = 'block'; app_path = 'p'; remote_addr = '127.0.0.1' } },
    @{ r = 'unknown layer'; s = @{ id = 'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66'; layer = 'connect-v9'; action = 'block'; app_path = 'p' } },
    @{ r = 'unknown key'; s = @{ id = 'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66'; layer = 'connect-v4'; action = 'block'; app_path = 'p'; evil = 1 } }
)
foreach ($b in $badSpecs) {
    Assert-Throws { Test-D4RuleSpec -RawSpec $b['s'] -Contract $contract } ('a bad rule spec refuses: ' + $b['r']) ''
}
Write-Host '== R67-A: same-id wrong semantics refuse (lib facts matrix) =='
foreach ($b in @(
    @{ r = 'wrong action'; f = "layer=$layerV4 action=0x1001 conds=3 appid=MATCH addr=2130706433 port=9100" },
    @{ r = 'wrong layer'; f = "layer=$layerV6 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=9100" },
    @{ r = 'appid mismatch'; f = "layer=$layerV4 action=0x1002 conds=3 appid=MISMATCH addr=2130706433 port=9100" },
    @{ r = 'narrowed conds'; f = "layer=$layerV4 action=0x1002 conds=2 appid=MATCH addr=2130706433 port=9100" },
    @{ r = 'wrong scope port'; f = "layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=8000" },
    @{ r = 'wrong scope addr'; f = "layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=1 port=9100" }
)) {
    $v = Test-D4RuleFactsMatch -Spec $rulePermit -FactsText $b['f'] -Contract $contract
    Assert-True (-not $v['Ok']) ('a same-id rule with ' + $b['r'] + ' refuses')
}
Assert-True ((Test-D4RuleFactsMatch -Spec $rulePermit -FactsText $factPermit -Contract $contract)['Ok']) 'the correct full matrix passes'
Assert-True ((Test-D4RuleFactsMatch -Spec $ruleBlockV6 -FactsText $factBlockV6 -Contract $contract)['Ok']) 'the correct block rule passes'

Write-Host '== R67-B: a FOREIGN same-id rule refuses takeover at S1 (zero rule takeover) =='
$os8 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch8 = New-D4FakeSchtasks -Oses $os8
$acl8 = New-D4HarnessAclRunner -Oses $os8
$foreignFacts = @{ $rulePermit['id'] = "layer=$layerV4 action=0x1001 conds=1 appid=MATCH addr=NA port=NA" }
$foreignStore = New-D4FakeRuleStore -CreatedFacts $createdFacts -SeedForeign $foreignFacts
$splat8 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch8.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $foreignStore.Runner -Os $os8 -Acl $acl8.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splat8 | Out-Null
Assert-Throws { & $entry -Stage 'S1' @splat8 } 'S1 refuses a FOREIGN same-id rule (no takeover)' 'FOREIGN specification; refusing takeover'
Assert-True ($foreignStore.State['Live'].ContainsKey($rulePermit['id'].ToLowerInvariant())) 'the foreign rule was KEPT (never overwritten)'
Assert-True ((@($sch8.State['Calls']) | Where-Object { $_ -match '/Run' }).Count -eq 0) 'the refused S1 started NOTHING'

Write-Host '== R67-A: a replaced rule refuses the E1 gate with zero starts =='
$os9 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch9 = New-D4FakeSchtasks -Oses $os9
$acl9 = New-D4HarnessAclRunner -Oses $os9
$tamperedFacts = @{ $rulePermit['id'] = "layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=8000" }
$tamperedStore = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splat9 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $sch9.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $tamperedStore.Runner -Os $os9 -Acl $acl9.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splat9 | Out-Null
& $entry -Stage 'S1' @splat9 | Out-Null
# The rule is REPLACED after S1 verified it (the realistic tamper window);
# the E1 gate re-reads the live facts and refuses the mismatch.
$tamperedStore.State['Live'][$rulePermit['id'].ToLowerInvariant()] = $tamperedFacts[$rulePermit['id']]
$runsBefore9 = @($sch9.State['Calls'] | Where-Object { $_ -match '/Run' }).Count
Assert-Throws { & $entry -Stage 'E1' @splat9 } 'E1 refuses a tampered rule (facts mismatch)' 'experiment limits not ready'
Assert-Equal ((@($sch9.State['Calls']) | Where-Object { $_ -match '/Run' }).Count) $runsBefore9 'E1 with a tampered rule performs ZERO starts'

Write-Host '== R67-B/C: the FULL closed loop - S1 prepares rules/ACL, Cleanup removes them in ONE pass =='
# Reuse the FIRST entry run's state (S0+S1 completed with $os/$splat):
# the shared fake store now holds THIS run's three rules; Cleanup must
# remove them (identity-checked, ABSENT confirmed), resolve every owned
# file/dir in ONE pass, and keep the evidence dir.
& $entry -Stage 'Cleanup' @splat | Out-Null
Assert-Equal $LASTEXITCODE 0 'cleanup exits 0 over the fully prepared run'
Assert-Equal $rules.State['Live'].Count 0 'the limit rules are ABSENT after cleanup (create/remove closed loop)'
$splatState = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
Assert-True (-not $os.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'the owned files are GONE after the one-pass cleanup'
Assert-True ((@($rules.State['Calls']) | Where-Object { $_ -match '^limits remove ' }).Count -ge 1) 'cleanup ran the identity-checked rule removal'
Assert-True ((@($rules.State['Calls']) | Where-Object { $_ -match '/Run' }).Count -eq 0) 'cleanup NEVER starts anything'
# Repeated Cleanup is idempotent (rules already absent, objects removed).
& $entry -Stage 'Cleanup' @splat | Out-Null
Assert-Equal $LASTEXITCODE 0 'a repeated cleanup is idempotent and passes'

# ---- R67-C: the deletion evidence gate + partial-failure recovery ---------------
Write-Host '== R67-C: empty/bad hash NEVER deletes; same-run recovery resolves =='
$osA = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schA = New-D4FakeSchtasks -Oses $osA
$aclA = New-D4HarnessAclRunner -Oses $osA
$storeA = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splatA = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schA.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeA.Runner -Os $osA -Acl $aclA.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatA | Out-Null
& $entry -Stage 'S1' @splatA | Out-Null
$ledgerA = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledgerText = Invoke-D4File -Boundary $osA.Boundary -Operation 'ReadText' -Arguments @($ledgerA)
$ledgerObj = $ledgerText | ConvertFrom-Json
$deployEntry = @($ledgerObj.entries | Where-Object { $_.path -like '*deploy-mode.json' })[0]
$origHash = [string]$deployEntry.sha256_actual
$deployEntry.sha256_actual = ''
Invoke-D4File -Boundary $osA.Boundary -Operation 'WriteText' -Arguments @($ledgerA, ($ledgerObj | ConvertTo-Json -Depth 6))
Assert-Throws { & $entry -Stage 'Cleanup' @splatA } 'an empty recorded hash is KEPT (never deleted on status alone)' 'cleanup incomplete'
Assert-True ($osA.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'the empty-hash file still EXISTS (zero deletion)'
Assert-True (-not $osA.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\share_entry.py').ToLowerInvariant())) 'the fingerprint-verified objects were removed'
$deployEntry.sha256_actual = $origHash
Invoke-D4File -Boundary $osA.Boundary -Operation 'WriteText' -Arguments @($ledgerA, ($ledgerObj | ConvertTo-Json -Depth 6))
& $entry -Stage 'Cleanup' @splatA | Out-Null
Assert-Equal $LASTEXITCODE 0 'the SAME run recovers and completes the cleanup after the record is resolved'
Assert-True (-not $osA.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'the resolved object is removed on the recovered cleanup'
$osB = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schB = New-D4FakeSchtasks -Oses $osB
$aclB = New-D4HarnessAclRunner -Oses $osB
$storeB = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splatB = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schB.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeB.Runner -Os $osB -Acl $aclB.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatB | Out-Null
& $entry -Stage 'S1' @splatB | Out-Null
# R78-A: the owned copies live in the fake OS's BYTE store - the tamper must
# replace the actual bytes (a Files-map write is invisible to the byte Hash).
$osB.State['Binaries'][($contract['ExperimentRoot'] + '\share_entry.py').ToLowerInvariant()] = [Text.Encoding]::UTF8.GetBytes('REPLACED-BY-SOMEONE-ELSE')
Assert-Throws { & $entry -Stage 'Cleanup' @splatB } 'a replaced object is KEPT and blocks the pass' 'cleanup incomplete'
Assert-Equal ([Text.Encoding]::UTF8.GetString($osB.State['Binaries'][($contract['ExperimentRoot'] + '\share_entry.py').ToLowerInvariant()])) 'REPLACED-BY-SOMEONE-ELSE' 'the replaced bytes are UNCHANGED (never taken over)'
$osB.State['Files'][($contract['ExperimentRoot'] + '\image\stranger.bin').ToLowerInvariant()] = 'FOREIGN'
Assert-Throws { & $entry -Stage 'Cleanup' @splatB } 'a non-empty dir with unknown content blocks the pass' 'cleanup incomplete'
Assert-Equal $osB.State['Files'][($contract['ExperimentRoot'] + '\image\stranger.bin').ToLowerInvariant()] 'FOREIGN' 'the unknown content is PRESERVED'

# ---- E1 (W2) share python_home read grant: grant+verify+ledger, then remove ----
Write-Host '== E1 python_home share grant: grant/verify/ledger, remove/ABSENT =='
$phOs = New-D4InMemoryOs -Root $tmpBase
$phAcl = New-D4HarnessAclRunner -Oses $phOs
$phLedgerPath = Join-Path $tmpBase 'intent-phg.json'
$phLedger = New-D4IntentLedger -Inventory $inventory -LedgerPath $phLedgerPath -FileBoundary $phOs.Boundary
# absent python_home grant -> the ledger has no ownership fact; a fresh grant
# is added (kind=acl) + verified read-back, then the remove reverses it.
$shareSidTest = 'S-1-5-21-3623811015-3361044348-30300820-1002'
$phHome = 'C:\PythonExp'
$g = Invoke-D4PrepareSharePythonHomeAccess -PythonHome $phHome -ShareSid $shareSidTest `
    -IcaclsRunner $phAcl.Runner -Ledger $phLedger -LedgerPath $phLedgerPath -FileBoundary $phOs.Boundary
$phLedger = $g['Ledger']
$aclRows = @($phAcl.State['Acl'][$phHome.ToLowerInvariant()])
Assert-True (($aclRows -join ';') -match [regex]::Escape('*' + $shareSidTest)) 'the share SID gained an ACE on the python home'
$phVer = Test-PythonHomeAceForShare -Output @(('C:\PythonExp ' + ($aclRows -join ' '))) -ShareSid $shareSidTest -PythonHome $phHome
Assert-True ($phVer['Ok']) 'the granted share ACE verifies as read+execute present'
Assert-True ((@($phLedger['entries'] | Where-Object { $_['path'] -eq 'acl:pythonhome' -and $_['status'] -eq 'created' }).Count) -eq 1) 'the python-home grant is ledger-owned (created)'
# The grant is idempotent (a second prepare keeps one created record).
$g2 = Invoke-D4PrepareSharePythonHomeAccess -PythonHome $phHome -ShareSid $shareSidTest `
    -IcaclsRunner $phAcl.Runner -Ledger $phLedger -LedgerPath $phLedgerPath -FileBoundary $phOs.Boundary
Assert-True ((@($g2['Ledger']['entries'] | Where-Object { $_['path'] -eq 'acl:pythonhome' -and $_['status'] -eq 'created' }).Count) -eq 1) 'the python-home grant stays a SINGLE owned record on reentry'
# Remove: identity-checked, ABSENT confirmed, ledger advanced.
$r = Invoke-D4RemoveSharePythonHomeAccess -PythonHome $phHome -ShareSid $shareSidTest `
    -IcaclsRunner $phAcl.Runner -Ledger $g2['Ledger'] -LedgerPath $phLedgerPath -FileBoundary $phOs.Boundary
$aclRows2 = @($phAcl.State['Acl'][$phHome.ToLowerInvariant()])
Assert-True (($aclRows2 -join ';') -notmatch [regex]::Escape('*' + $shareSidTest)) 'the share ACE is GONE after the remove'
$phAbs = Test-PythonHomeAceForShare -Output @(('C:\PythonExp ' + ($aclRows2 -join ' '))) -ShareSid $shareSidTest -PythonHome $phHome
Assert-True (-not $phAbs['Ok'] -and $phAbs['State'] -eq 'absent') 'the share ACE absence is explicitly ABSENT (not just unconfirmed)'
Assert-True ((@($r['Ledger']['entries'] | Where-Object { $_['path'] -eq 'acl:pythonhome' -and $_['status'] -eq 'removed' }).Count) -eq 1) 'the python-home grant record advanced to removed'
# A foreign grant (no THIS-run created record) is KEPT, never removed.
$phOs2 = New-D4InMemoryOs -Root $tmpBase
$phAcl2 = New-D4HarnessAclRunner -Oses $phOs2
$phLedgerPath2 = Join-Path $tmpBase 'intent-phg2.json'
$phLedger2 = New-D4IntentLedger -Inventory $inventory -LedgerPath $phLedgerPath2 -FileBoundary $phOs2.Boundary
$rmF = Invoke-D4RemoveSharePythonHomeAccess -PythonHome $phHome -ShareSid $shareSidTest `
    -IcaclsRunner $phAcl2.Runner -Ledger $phLedger2 -LedgerPath $phLedgerPath2 -FileBoundary $phOs2.Boundary
Assert-True ((@($rmF['KeptForeign']).Count) -eq 1) 'an ungranted python-home ACE is KEPT (no THIS-run ownership fact)'
Assert-True ((@($phAcl2.State['Calls'] | Where-Object { $_ -match '/remove' }).Count) -eq 0) 'a kept foreign grant makes ZERO /remove calls'

# ---- R69-A: the REAL codec adapter fixture (controlled memory, no OS) ---------
Write-Host '== R69-A: codec encode/decode roundtrip + call contract =='
# Compile the delivered adapter types via the REAL runner factory (Add-Type
# only - no OS call; the fixture then drives the compiled codec directly).
[void](New-D4RealRulesRunner -RuleSpecs $ruleSpecs -Contract $contract)
$codecAppId = [Text.Encoding]::Unicode.GetBytes('C:\ProgramData\UMShareExp-broker\um-share-python.exe'.ToLowerInvariant())
$layerV4g = [Guid]'c38d57d1-05a7-4c33-904f-7fbceee60e82'
$layerV6g = [Guid]'4a72393b-319f-44bc-84c3-ba54dcb3b6b4'
# PERMIT: build the native buffer with the REAL codec, decode with the REAL
# reader, and assert the facts AND the raw layout bytes at the reviewed
# offsets - the delivered encode/decode logic runs, not a fabricated string.
$nf = New-Object UmD4RuleCodec+NativeFilter(
    [Guid]'9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55', 'um-share-exp-permit-9f3a7c42',
    $layerV4g, [UmD4RuleCodec]::ACT_PERMIT, $codecAppId, $true,
    [uint32]2130706433, [uint16]9100, [UmD4RuleCodec]::SubUniversal)
try {
    $facts = [UmD4RuleCodec]::Facts($nf.Buffer, $codecAppId)
    Assert-Equal $facts "layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=9100" 'permit codec roundtrip facts'
    $lb = New-Object byte[] 16
    [void][Runtime.InteropServices.Marshal]::Copy(
        [IntPtr]($nf.Buffer.ToInt64() + 0x40), $lb, 0, 16)
    Assert-Equal ([Guid]$lb).ToString() $layerV4g.ToString() 'the layer GUID lands at the reviewed offset 0x40'
    Assert-Equal ([Runtime.InteropServices.Marshal]::ReadInt32($nf.Buffer, 0x80)) 0x1002 'the action code lands at 0x80'
    Assert-Equal ([Runtime.InteropServices.Marshal]::ReadInt32($nf.Buffer, 0x70)) 3 'the condition count lands at 0x70'
    # The scalar condition values are INLINE (never pointer-dereferenced):
    # addr @cond1+0x20, port @cond2+0x20.
    $condBase = [Runtime.InteropServices.Marshal]::ReadIntPtr($nf.Buffer, 0x78)
    Assert-Equal ([Runtime.InteropServices.Marshal]::ReadInt32($condBase, 0x18)) 12 'the app-id condition value type is BYTE_BLOB(12)'
    Assert-Equal ([Runtime.InteropServices.Marshal]::ReadInt32(
        [IntPtr]($condBase.ToInt64() + 40), 0x18)) 3 'the addr condition value type is UINT32(3) (inline)'
    Assert-Equal ([Runtime.InteropServices.Marshal]::ReadInt32(
        [IntPtr]($condBase.ToInt64() + 40), 0x20)) 2130706433 'the addr scalar is stored INLINE at the union'
    Assert-True (([Runtime.InteropServices.Marshal]::ReadInt16(
        [IntPtr]($condBase.ToInt64() + 80), 0x20)) -eq 9100) 'the port scalar is stored INLINE at the union'
    # A tampered app-id blob must decode as MISMATCH (the byte comparison).
    $badAppId = [Text.Encoding]::Unicode.GetBytes('c:\other\image.exe')
    $factsBad = [UmD4RuleCodec]::Facts($nf.Buffer, $badAppId)
    Assert-True ($factsBad -match 'appid=MISMATCH') 'a tampered app-id decodes as MISMATCH'
} finally {
    $nf.Dispose()
}
# BLOCK: exactly ONE condition (app-id only); scope fields never read.
$nfB = New-Object UmD4RuleCodec+NativeFilter(
    [Guid]'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66', 'um-share-exp-block-a1c4e8f7',
    $layerV4g, [UmD4RuleCodec]::ACT_BLOCK, $codecAppId, $false,
    [uint32]0, [uint16]0, [UmD4RuleCodec]::SubUniversal)
try {
    Assert-Equal ([UmD4RuleCodec]::Facts($nfB.Buffer, $codecAppId)) "layer=$layerV4 action=0x1001 conds=1 appid=MATCH addr=NA port=NA" 'block codec roundtrip facts (app-id only)'
} finally {
    $nfB.Dispose()
}
# Dispose frees the buffer: a second Dispose is a safe no-op; the buffer is
# zero-length accessible? (we only assert Dispose is idempotent - no throw).
$nf.Dispose()
Assert-True $true 'codec Dispose is idempotent (allocation ownership closed)'
# The P/Invoke surface: the out-parameter BINDING is verified by reflection
# (the real fwpuclnt call stays D4-X; the signature must promise it).
$addMethod = [UmD4RuleOps].GetMethod('AddIsolationFilter')
Assert-Equal $addMethod.GetParameters().Count 3 'AddIsolationFilter binds (engine, native, out id)'
Assert-True ($addMethod.GetParameters()[2].ParameterType.IsByRef) 'the id parameter is an OUT (by-ref) binding'
Assert-Equal $addMethod.GetParameters()[1].ParameterType.FullName 'UmD4RuleCodec+NativeFilter' 'AddIsolationFilter consumes the codec-built native filter'
$getByKey = [UmD4RuleOps].GetMethod('FwpmFilterGetByKey0')
Assert-True ($getByKey.GetParameters()[1].ParameterType.IsByRef -and
             $getByKey.GetParameters()[2].ParameterType.IsByRef) 'GetByKey binds the key and the filter by ref'

# ---- R67-A arbitration + lazy normalization (W2 run20) ------------------------
Write-Host '== R67-A: permit weight outranks block; list never normalizes =='
# Fix A: with EXPLICIT equal weights the engine's tie-break prefers BLOCK
# over PERMIT, so the scoped permit was unreachable and the app was fully
# contained (the W2 run20 WSAEACCES 10013 on the permitted loopback connect).
$nfPw = New-Object UmD4RuleCodec+NativeFilter(
    [Guid]'b7d2a1e3-4c58-4f96-9a20-3e5c7d8b1a44', 'um-share-exp-permit-b7d2a1e3',
    $layerV4g, [UmD4RuleCodec]::ACT_PERMIT, $codecAppId, $true,
    [uint32]2130706433, [uint16]9100, [UmD4RuleCodec]::SubUniversal)
$nfBw = New-Object UmD4RuleCodec+NativeFilter(
    [Guid]'c8e3b2f4-5d69-4a07-8b31-4f6d8e9c2b55', 'um-share-exp-block-c8e3b2f4',
    $layerV4g, [UmD4RuleCodec]::ACT_BLOCK, $codecAppId, $false,
    [uint32]0, [uint16]0, [UmD4RuleCodec]::SubUniversal)
try {
    $wPermit = [Runtime.InteropServices.Marshal]::ReadInt64(
        [Runtime.InteropServices.Marshal]::ReadIntPtr($nfPw.Buffer, 0x68))
    $wBlock = [Runtime.InteropServices.Marshal]::ReadInt64(
        [Runtime.InteropServices.Marshal]::ReadIntPtr($nfBw.Buffer, 0x68))
    Assert-Equal $wBlock ([int64]0x0000F00000000000) 'the block keeps the base weight'
    Assert-Equal $wPermit ([int64]0x0000F00000000001) 'the scoped permit carries base+1'
    Assert-True ($wPermit -gt $wBlock) 'the permit outranks the block (the tie-break can no longer contain the scoped destination)'
} finally {
    $nfPw.Dispose(); $nfBw.Dispose()
}
# Fix B: the 'list' verb never consumes the app-id, so the cleanup's final
# verdict (image files ALREADY REMOVED; W2 run20 normalization 0x2) must
# still produce per-id verdict lines; a verb that DOES consume the app-id
# refuses loudly on the same broken normalization.
$brokenNorm = New-D4FakeNativeOps
$brokenOps = @{
    EngineOpen = $brokenNorm.Ops['EngineOpen']
    GetByKey   = $brokenNorm.Ops['GetByKey']
    Add        = $brokenNorm.Ops['Add']
    Delete     = $brokenNorm.Ops['Delete']
    Free       = $brokenNorm.Ops['Free']
    Close      = $brokenNorm.Ops['Close']
    GetAppId   = { param([string]$AppPath, [ref]$Blob)
                   $brokenNorm.State['Calls'].Add('GetAppId')
                   return [uint32]2 }.GetNewClosure()
}
$brokenRunner = New-D4RealRulesRunner -RuleSpecs $ruleSpecs -Contract $contract -NativeOps $brokenOps
$bl = Invoke-CommandRunner -Runner $brokenRunner -ArgumentList (@('limits', 'list') + $ruleIds)
Assert-Equal $bl.ExitCode 0 "a broken normalization still lets 'list' produce verdicts (lazy gate)"
Assert-Equal @($bl.Output).Count 3 'list emitted one verdict line per requested id'
Assert-True ((@($bl.Output) | Where-Object { $_ -like 'ABSENT *' }).Count -eq 3) 'the empty fake store yields explicit ABSENT verdict lines'
Assert-True ((@($brokenNorm.State['Calls']) | Where-Object { $_ -eq 'GetAppId' }).Count -eq 0) "the 'list' verb performs ZERO GetAppId calls"
$bv = Invoke-CommandRunner -Runner $brokenRunner -ArgumentList (@('limits', 'verify') + @($ruleIds[0]))
Assert-Equal $bv.ExitCode 2 'a normalization-consuming verb still refuses loudly on the broken normalization (exit 2)'

# ---- R69-B: same-spec foreign + delete-confirm evidence ------------------------
Write-Host '== R69-B: same-spec is not ownership; delete confirm gates =='
$osT = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schT = New-D4FakeSchtasks -Oses $osT
$aclT = New-D4HarnessAclRunner -Oses $osT
$matchingStore = New-D4FakeRuleStore -CreatedFacts $createdFacts -SeedLive $createdFacts
$splatT = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schT.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $matchingStore.Runner -Os $osT -Acl $aclT.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatT | Out-Null
Assert-Throws { & $entry -Stage 'S1' @splatT } 'a same-spec pre-existing rule refuses takeover' 'already existed BEFORE this run'
Assert-True $matchingStore.State['Live'].ContainsKey($rulePermit['id'].ToLowerInvariant()) 'the pre-existing rule was KEPT'
$osD = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schD = New-D4FakeSchtasks -Oses $osD
$aclD = New-D4HarnessAclRunner -Oses $osD
$storeD = New-D4FakeRuleStore -CreatedFacts $createdFacts
$flakyStore = @{
    Runner = {
        param([string[]]$argv)
        # Fail the verify ONLY after the removal (the post-delete confirm):
        # while the rule is still live the wrapper delegates unchanged.
        if ($argv[1] -eq 'verify' -and $argv[2] -eq $ruleBlockV6['id'] -and
                -not $storeD.State['Live'].ContainsKey($ruleBlockV6['id'].ToLowerInvariant())) {
            return (New-TestRunResult -ExitCode 2 -Output @('QUERY-FAILED net error'))
        }
        return (& $storeD.Runner @($argv))
    }.GetNewClosure()
}
$splatD = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schD.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $flakyStore.Runner -Os $osD -Acl $aclD.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatD | Out-Null
& $entry -Stage 'S1' @splatD | Out-Null
Assert-Throws { & $entry -Stage 'Cleanup' @splatD } 'a post-delete query FAILURE refuses (ledger NOT advanced)' 'post-delete state UNCONFIRMED'
# The REAL store did delete the filter, but the LEDGER keeps created -
# the post-delete state is UNCONFIRMED, never advanced to removed.
$ledgerD = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledgerTextD = Invoke-D4File -Boundary $osD.Boundary -Operation 'ReadText' -Arguments @($ledgerD)
$ledgerObjD = $ledgerTextD | ConvertFrom-Json
$blockV6Entry = @($ledgerObjD.entries | Where-Object { $_.path -eq ('rule:' + $ruleBlockV6['id']) })[0]
Assert-Equal ([string]$blockV6Entry.status) 'created' 'the unconfirmed rule is NOT recorded removed'

# ---- R69-C: the ACL recovery closure (state persists through FileBoundary) -----
Write-Host '== R69-C: recovery persists none/failed; corrupt refuses =='
$osR = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$statePathR = Join-Path (Join-Path $contract['ExperimentRoot'] 'evidence') 'acl-recovery.json'
$stateObjR = @{ dirs = @(@{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osR.Boundary -Operation 'WriteText' -Arguments @($statePathR, ($stateObjR | ConvertTo-Json -Depth 5))
$emptyRunner = {
    param([string[]]$argv)
    if ($argv -contains '/restore') { return (New-TestRunResult -ExitCode 0 -Output @('restored')) }
    return (New-TestRunResult -ExitCode 0 -Output @(''))
}.GetNewClosure()
$r = Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $emptyRunner `
    -FileBoundary $osR.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($r['Unconfirmed']).Count 1 'a missing backup is UNCONFIRMED (never confirmed)'
$stateR = Invoke-D4File -Boundary $osR.Boundary -Operation 'ReadText' -Arguments @($statePathR) | ConvertFrom-Json
Assert-Equal ([string]$stateR.dirs[0].state) 'failed' 'the missing-backup dir persists state=failed'
Invoke-D4File -Boundary $osR.Boundary -Operation 'WriteText' -Arguments @($statePathR, '{ broken')
Assert-Throws { Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $emptyRunner `
    -FileBoundary $osR.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'a corrupt recovery record REFUSES' 'not valid JSON'
$osS = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$statePathS = Join-Path (Join-Path $contract['ExperimentRoot'] 'evidence') 'acl-recovery.json'
$stateObjS = @{ dirs = @(@{ path = (Join-Path $contract['ExperimentRoot'] 'run\live'); backup_rel = 'acl-backup-run-live.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osS.Boundary -Operation 'WriteText' -Arguments @($statePathS, ($stateObjS | ConvertTo-Json -Depth 5))
Invoke-D4File -Boundary $osS.Boundary -Operation 'WriteText' -Arguments @(
    (Join-Path (Join-Path $contract['ExperimentRoot'] 'evidence') 'acl-backup-run-live.dat'), 'SIMULATED-ACL')
$okRunner = {
    param([string[]]$argv)
    return (New-TestRunResult -ExitCode 0 -Output @('restored'))
}.GetNewClosure()
$r2 = Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osS.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($r2['Confirmed']).Count 1 'a successful restore is CONFIRMED'
$stateS = Invoke-D4File -Boundary $osS.Boundary -Operation 'ReadText' -Arguments @($statePathS) | ConvertFrom-Json
Assert-Equal ([string]$stateS.dirs[0].state) 'none' 'the successful restore persists state=none'

# ---- R69-D: the consumer matrix + missing limited-write principal ---------------
Write-Host '== R69-D: um_share consumer grants + missing-principal refusal =='
$runLiveAcl = @(
    (Join-Path $contract['ExperimentRoot'] 'run\live'),
    'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
    'BUILTIN\Administrators:(OI)(CI)(F)',
    'dacint:(OI)(CI)(RX)',
    '*S-1-5-21-1002:(OI)(CI)(M)')
$v = Test-AclOutput -Output $runLiveAcl -TargetPath (Join-Path $contract['ExperimentRoot'] 'run\live') `
    -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @() `
    -LimitedWrite @{ '*S-1-5-21-1002' = @('M') }
Assert-True $v['Ok'] 'the run\live consumer matrix passes (um_share registration set)'
$missingAcl = @(
    (Join-Path $contract['ExperimentRoot'] 'run\live'),
    'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
    'BUILTIN\Administrators:(OI)(CI)(F)')
$v = Test-AclOutput -Output $missingAcl -TargetPath (Join-Path $contract['ExperimentRoot'] 'run\live') `
    -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @() `
    -LimitedWrite @{ '*S-1-5-21-1002' = @('M') }
Assert-True (-not $v['Ok']) 'a missing limited-write principal REFUSES'
Assert-True ($v['Reason'] -match 'missing') 'the missing-principal reason names the identity'

# ---- R71-A: creation race + allocation-failure release balance ------------------
Write-Host '== R71-A: the race is never owned; ctor failure frees the balance =='
$osRace = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schRace = New-D4FakeSchtasks -Oses $osRace
$aclRace = New-D4HarnessAclRunner -Oses $osRace
$raceStore = @{
    Runner = {
        param([string[]]$argv)
        if ($argv[1] -eq 'create') {
            return (New-TestRunResult -ExitCode 0 -Output @(
                'ALREADY-PRESENT ' + $argv[2] + " layer=$layerV4 action=0x1002 conds=3 appid=MATCH addr=2130706433 port=9100"))
        }
        return (New-TestRunResult -ExitCode 1 -Output @('ABSENT ' + $argv[2]))
    }.GetNewClosure()
}
$splatRace = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schRace.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $raceStore.Runner -Os $osRace -Acl $aclRace.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatRace | Out-Null
Assert-Throws { & $entry -Stage 'S1' @splatRace } 'the race result is NEVER recorded as created (ledger gate)' 'already existed BEFORE this run'
$liveBase = [UmD4RuleCodec+NativeFilter]::LiveAllocations
$failN = 3
$savedAllocator = [UmD4RuleCodec+NativeFilter]::Allocator
[UmD4RuleCodec+NativeFilter]::Allocator = {
    param([int]$size)
    $script:allocN = ($script:allocN + 1)
    if ($script:allocN -ge $failN) { throw [OutOfMemoryException]::new('injected: allocation failed') }
    return [Runtime.InteropServices.Marshal]::AllocHGlobal($size)
}
$script:allocN = 0
try {
    $threw = $false
    try {
        [void](New-Object UmD4RuleCodec+NativeFilter(
            [Guid]'9f3a7c42-5d1e-4b8a-9c2f-6a7d0e1b3c55', 'injected-failure',
            $layerV4g, [UmD4RuleCodec]::ACT_PERMIT, $codecAppId, $true,
            [uint32]2130706433, [uint16]9100, [UmD4RuleCodec]::SubUniversal))
    } catch {
        # New-Object WRAPS the ctor's OutOfMemoryException into a
        # MethodInvocationException - the failure is identified by the
        # message chain, not by the wrapper type.
        $threw = ("$($_.Exception.Message)$($_.Exception.InnerException.Message)" -match
            'injected: allocation failed')
    }
    Assert-True $threw 'the injected allocation failure THROWS from the constructor'
    Assert-Equal ([UmD4RuleCodec+NativeFilter]::LiveAllocations) $liveBase 'the failed ctor freed every registered block (balance restored)'
} finally {
    [UmD4RuleCodec+NativeFilter]::Allocator = $savedAllocator
}
$nf2 = New-Object UmD4RuleCodec+NativeFilter(
    [Guid]'a1c4e8f7-2b39-4d56-8e70-1f9a2b3c4d66', 'balance-check',
    $layerV4g, [UmD4RuleCodec]::ACT_BLOCK, $codecAppId, $false,
    [uint32]0, [uint16]0, [UmD4RuleCodec]::SubUniversal)
Assert-True ([UmD4RuleCodec+NativeFilter]::LiveAllocations -gt $liveBase) 'a successful build holds its allocations'
$nf2.Dispose()
Assert-Equal ([UmD4RuleCodec+NativeFilter]::LiveAllocations) $liveBase 'Dispose returns the balance to the baseline'

# ---- R73: the recovery record closure (full-set persistence + one contract) ----
Write-Host '== R73-A: an interrupted item NEVER erases the pending obligation =='
$osX = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$statePathX = Join-Path (Join-Path $contract['ExperimentRoot'] 'evidence') 'acl-recovery.json'
$stateObjX = @{ dirs = @(
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'pending' },
    @{ path = (Join-Path $contract['ExperimentRoot'] 'run\live'); backup_rel = 'acl-backup-run-live.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($stateObjX | ConvertTo-Json -Depth 5))
# The backups MUST exist for the restore to attempt an icacls (the record
# pre-validation passed; a missing backup would legitimately persist failed).
$evX = Join-Path $contract['ExperimentRoot'] 'evidence'
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @(
    (Join-Path $evX 'acl-backup-root.dat'), 'SIMULATED-ROOT-ACL')
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @(
    (Join-Path $evX 'acl-backup-run-live.dat'), 'SIMULATED-RUN-LIVE-ACL')
# Root restores successfully; the runner then THROWS on the second item
# (a deterministic mid-recovery abort - process dies before run\live).
$abortRunner = {
    param([string[]]$argv)
    if ($argv -contains '/restore' -and $argv[1] -eq (Join-Path $contract['ExperimentRoot'] 'run\live')) {
        throw [InvalidOperationException]::new('injected: process died before run\live restore')
    }
    return (New-TestRunResult -ExitCode 0 -Output @('restored'))
}.GetNewClosure()
try {
    [void](Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $abortRunner `
        -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
        -ExperimentRoot $contract['ExperimentRoot'])
} catch [InvalidOperationException] {
    # the injected abort - expected
}
# The on-disk record must contain BOTH facts: root=none AND run\live=pending.
$afterAbort = Invoke-D4File -Boundary $osX.Boundary -Operation 'ReadText' -Arguments @($statePathX) | ConvertFrom-Json
$rootEntry = @($afterAbort.dirs | Where-Object { $_.path -eq $contract['ExperimentRoot'] })[0]
$runEntry = @($afterAbort.dirs | Where-Object { $_.path -like '*run\live' })[0]
Assert-Equal ([string]$rootEntry.state) 'none' 'the completed item persisted its own fact'
Assert-Equal ([string]$runEntry.state) 'pending' 'the interrupted item KEPT its pending obligation on disk'
# A NEW instance resumes: only the pending item is restored (root=none stays).
$rX = Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($rX['Confirmed']).Count 1 'the new instance restores ONLY the remaining item'
$afterResume = Invoke-D4File -Boundary $osX.Boundary -Operation 'ReadText' -Arguments @($statePathX) | ConvertFrom-Json
Assert-Equal ([string]@($afterResume.dirs | Where-Object { $_.path -eq $contract['ExperimentRoot'] })[0].state) 'none' 'the root fact is UNCHANGED by the resume'
Assert-Equal ([string]@($afterResume.dirs | Where-Object { $_.path -like '*run\live' })[0].state) 'none' 'the resumed item reached none'
# R75.2: the third restore owes NOTHING - zero icacls calls, the record
# keeps exactly two entries with unique targets (the skip branch can never
# grow the collection), and Complete parses it normally.
$icaclsCallsBefore3rd = @($osX.State['Calls']).Count
$r3 = Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal (@($osX.State['Calls']).Count - $icaclsCallsBefore3rd) 0 'the third restore makes ZERO icacls calls (nothing owed)'
$after3rd = Invoke-D4File -Boundary $osX.Boundary -Operation 'ReadText' -Arguments @($statePathX) | ConvertFrom-Json
Assert-Equal @($after3rd.dirs).Count 2 'the record still carries EXACTLY the two targets (skip never grows)'
Assert-Equal @($after3rd.dirs | ForEach-Object { $_.path.ToLowerInvariant() } | Select-Object -Unique).Count 2 'the two targets are UNIQUE after the skips'
$c3 = Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($c3['Removed']).Count 0 'the finalize over none states claims zero removals'

Write-Host '== R73-B: ONE record contract at BOTH entries (negatives) =='
# Missing dirs: corrupt, never an empty state - Restore AND Complete refuse.
$badNoDirs = '{"something": 1}'
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, $badNoDirs)
Assert-Throws { Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Restore refuses a record without dirs' 'missing the dirs list'
Assert-Throws { Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Complete refuses a record without dirs' 'missing the dirs list'
# Swapped backups: root carries run\live's backup - refused by the pairing.
$swapped = @{ dirs = @(
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-run-live.dat'; state = 'pending' },
    @{ path = (Join-Path $contract['ExperimentRoot'] 'run\live'); backup_rel = 'acl-backup-root.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($swapped | ConvertTo-Json -Depth 5))
Assert-Throws { Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Restore refuses a swapped backup pairing' 'PAIRING mismatch'
Assert-Throws { Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Complete refuses a swapped backup pairing' 'PAIRING mismatch'
# Duplicate targets refuse.
$dups = @{ dirs = @(
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'pending' },
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($dups | ConvertTo-Json -Depth 5))
Assert-Throws { Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Restore refuses duplicate targets' 'DUPLICATE'
Assert-Throws { Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Complete refuses duplicate targets' 'DUPLICATE'
# Unknown state refuses at BOTH entries.
$badState = @{ dirs = @(
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'weird' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($badState | ConvertTo-Json -Depth 5))
Assert-Throws { Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Restore refuses an unknown state' 'unknown state'
Assert-Throws { Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot'] } 'Complete refuses an unknown state' 'unknown state'
# A legal record passes BOTH entries identically (no negative drift).
$good = @{ dirs = @(
    @{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'pending' },
    @{ path = (Join-Path $contract['ExperimentRoot'] 'run\live'); backup_rel = 'acl-backup-run-live.dat'; state = 'pending' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($good | ConvertTo-Json -Depth 5))
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @(
    (Join-Path $evX 'acl-backup-root.dat'), 'SIMULATED-ROOT-ACL')
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @(
    (Join-Path $evX 'acl-backup-run-live.dat'), 'SIMULATED-RUN-LIVE-ACL')
$rGood = Invoke-D4RestoreExperimentAcl -Cfg $config -IcaclsRunner $okRunner `
    -FileBoundary $osX.Boundary -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($rGood['Confirmed']).Count 2 'Restore accepts the legal record'
$cGood = Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($cGood['Removed']).Count 0 'Complete accepts the legal record (none states are not removed claims)'
# evidence retention: the root survives -> the applied entry stays applied
# (and the finalize NEVER claims removed over a kept dir).
$appliedObj = @{ dirs = @(@{ path = $contract['ExperimentRoot']; backup_rel = 'acl-backup-root.dat'; state = 'applied' }) }
Invoke-D4File -Boundary $osX.Boundary -Operation 'WriteText' -Arguments @($statePathX, ($appliedObj | ConvertTo-Json -Depth 5))
# Evidence retention: the root dir SURVIVES in the fake os (the evidence
# dir is kept) - the finalize must therefore report kept-applied.
$osX.State['Dirs'][($contract['ExperimentRoot']).ToLowerInvariant()] = $true
$cApplied = Invoke-D4CompleteExperimentAcl -FileBoundary $osX.Boundary `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -ExperimentRoot $contract['ExperimentRoot']
Assert-Equal @($cApplied['Removed']).Count 0 'evidence retention: the surviving root is NEVER claimed removed'
Assert-Equal @($cApplied['Kept']).Count 1 'the surviving root is REPORTED as kept-applied'
$afterKeep = Invoke-D4File -Boundary $osX.Boundary -Operation 'ReadText' -Arguments @($statePathX) | ConvertFrom-Json
Assert-Equal ([string]$afterKeep.dirs[0].state) 'applied' 'the kept dir PERSISTS its applied state truthfully'

# ---- R78 acceptance: byte-exact copy, real-manifest import, identity
# ---- traversal, rule-SET validation (offline, fake OS/native) ----------
Write-Host '== R78-A: NUL/invalid-UTF8/BOM bytes survive the S1 copy with hash identity =='
# Every byte value 0..255 in one file: NUL (0x00), invalid UTF-8 continuation
# bytes (0x80-0xBF) and a mid-file BOM (EF BB BF @ 239..241). A TEXT
# round-trip decodes the invalid bytes to U+FFFD and re-encodes them, so its
# hash can never equal the byte-exact copy's - the fixture is
# corruption-sensitive (a passing S1 proves the CopyBytes path).
$binSrcDir = Join-Path $tmpBase 'bin-src'
New-Item -ItemType Directory -Path $binSrcDir -Force | Out-Null
$binSrcFile = Join-Path $binSrcDir 'mixed-bytes.bin'
$rawBytes = New-Object byte[] 256
for ($i = 0; $i -lt 256; $i++) { $rawBytes[$i] = [byte]$i }
[IO.File]::WriteAllBytes($binSrcFile, $rawBytes)
$binSha = (Get-FileSha256 $binSrcFile).ToLowerInvariant()
$shaTmp = [System.Security.Cryptography.SHA256]::Create()
try {
    $textBytes = [Text.Encoding]::UTF8.GetBytes([Text.Encoding]::UTF8.GetString($rawBytes))
    $textSha = ([BitConverter]::ToString($shaTmp.ComputeHash($textBytes)) -replace '-', '').ToLowerInvariant()
} finally { $shaTmp.Dispose() }
Assert-True ($binSha -ne $textSha) 'the byte fixture is text-corruption-sensitive (a text round-trip cannot pass the hash gate)'
$osBin = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schBin = New-D4FakeSchtasks -Oses $osBin
$aclBin = New-D4HarnessAclRunner -Oses $osBin
$storeBin = New-D4FakeRuleStore -CreatedFacts $createdFacts
$cfgBinPath = Join-Path $tmpBase 'entry-config-bin.json'
$cfgBin = New-D4TestConfig -RunId $runId -RuntimeSourceDir $binSrcDir -RuntimeFiles @(@{ file = 'mixed-bytes.bin'; sha256 = $binSha })
[IO.File]::WriteAllText($cfgBinPath, ($cfgBin | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$pkgBin = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot `
    -RuntimeManifestJson ([string]$cfgBin['runtime_source_dir'] + '|' + [string]$cfgBin['python_home'] + '|' + 'mixed-bytes.bin=' + $binSha)
$splatBin = New-D4EntrySplat -RunId $runId -ConfigPath $cfgBinPath -Schtasks $schBin.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeBin.Runner -Os $osBin -Acl $aclBin.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatBin | Out-Null
& $entry -Stage 'S1' @splatBin | Out-Null
$binDst = Join-Path $contract['ExperimentRoot'] 'mixed-bytes.bin'
Assert-Equal (Invoke-D4File -Boundary $osBin.Boundary -Operation 'Hash' -Arguments @($binDst)) $binSha 'the copied bytes are IDENTICAL to the source (hash equality through the S1 copy path)'
$ledgerBinPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkgBin -RunId $runId
$ledgerBin = Invoke-D4File -Boundary $osBin.Boundary -Operation 'ReadText' -Arguments @($ledgerBinPath) | ConvertFrom-Json
$binEntry = @($ledgerBin.entries | Where-Object { $_.path -eq $binDst })[0]
Assert-Equal ([string]$binEntry.sha256_actual) $binSha 'the byte copy records its ACTUAL hash (the cleanup deletion basis)'

Write-Host '== R78-A boundary: the REAL CopyBytes is byte-exact and refuses takeover =='
# Isolated temp byte files (no experiment root, no system state): the REAL
# boundary's CopyBytes must carry NUL/invalid-UTF8/BOM bytes unchanged and
# refuse an existing destination (the boundary-level no-takeover guard,
# independent of S1's own pre-write refusal).
$rbDir = Join-Path $tmpBase 'real-boundary'
New-Item -ItemType Directory -Path $rbDir -Force | Out-Null
$rbSrc = Join-Path $rbDir 'src.bin'
[IO.File]::WriteAllBytes($rbSrc, $rawBytes)
$rbDst = Join-Path $rbDir 'dst.bin'
$rb = New-D4RealFileBoundary
Invoke-D4File -Boundary $rb -Operation 'CopyBytes' -Arguments @($rbSrc, $rbDst)
Assert-Equal ((Get-FileSha256 $rbDst).ToLowerInvariant()) $binSha 'the REAL boundary copy is byte-exact for NUL/invalid-UTF8/BOM bytes'
Assert-Throws { Invoke-D4File -Boundary $rb -Operation 'CopyBytes' -Arguments @($rbSrc, $rbDst) } 'the REAL boundary refuses an existing destination (no takeover)' 'no takeover'
Assert-Throws { Invoke-D4File -Boundary $osBin.Boundary -Operation 'CopyBytes' -Arguments @($binSrcFile, $binDst) } 'the FAKE boundary refuses an existing destination too' 'no takeover'
Remove-Item -LiteralPath $rbSrc -Force
Remove-Item -LiteralPath $rbDst -Force

Write-Host '== R78-B: the share identity travels S1 config -> broker config -> wrapper argv =='
# operator (dacint / S-1-5-21-...-1001) and share (um_share / S-1-5-21-...-1002)
# are DISTINCT collected identities; the entry's S1 must bind the BROKER
# config's owner slots to the SHARE identity and the share task principal to
# the share SID - never the operator's.
$osI = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schI = New-D4FakeSchtasks -Oses $osI
$aclI = New-D4HarnessAclRunner -Oses $osI
$storeI = New-D4FakeRuleStore -CreatedFacts $createdFacts
$cfgCaptures = New-Object System.Collections.Generic.List[hashtable]
$identityInstaller = {
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [scriptblock]$IcaclsRunner, [hashtable]$Boundary)
    $cfgCaptures.Add($Cfg)
    Invoke-D4File -Boundary $Boundary -Operation 'MakeDir' -Arguments @(
        (Join-Path $Cfg['RunRoot'] 'broker'))
    $mods = @()
    foreach ($rel in @('broker\hook-fixture.py', 'broker\broker-control-key',
                       'instance-channel-key', 'run-broker.cmd',
                       'broker\task-definition.xml')) {
        $dst = Join-Path $Cfg['RunRoot'] $rel
        Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
            $dst, ('HOOK-FIXTURE ' + $rel))
        $mods += @{ file = $rel;
                    sha256 = (Invoke-D4File -Boundary $Boundary -Operation 'Hash' -Arguments @($dst));
                    classification = 'created' }
    }
    $mods += @{ file = 'broker\pre-existing.dll'; sha256 = ''; classification = 'shared' }
    Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
        (Join-Path (Join-Path $Cfg['RunRoot'] 'broker') 'install-state.json'),
        (@{ task_name = $Cfg['TaskName']; modules = $mods } | ConvertTo-Json -Depth 4))
}.GetNewClosure()
$splatI = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schI.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeI.Runner -Os $osI -Acl $aclI.Runner `
    -Installer $identityInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatI | Out-Null
& $entry -Stage 'S1' @splatI | Out-Null
Assert-Equal $cfgCaptures.Count 1 'the entry built ONE broker config through the reviewed New-BrokerConfig'
$cfgB = $cfgCaptures[0]
Assert-Equal ([string]$cfgB['InstanceOwnerSid']) ([string]$entryConfig['share_sid']) 'the instance owner SID is the SHARE identity (never empty)'
Assert-Equal ([string]$cfgB['ExpectedOwnerAccount']) ([string]$entryConfig['share_account']) 'the expected owner account is the SHARE account'
Assert-True ([string]$cfgB['InstanceOwnerSid'] -ne ([string]$entryConfig['operator_sid'])) 'the share SID is DISTINCT from the operator SID'
Assert-True ([string]$cfgB['ExpectedOwnerAccount'] -ne ([string]$entryConfig['operator_account'])) 'the share account is DISTINCT from the operator account'
$wrapArgv = Get-BrokerWrapperServerArgv -WrapperContent (New-BrokerWrapperContent -Cfg $cfgB) -TargetRoot $contract['ExperimentRoot']
$oi = 0; while ($wrapArgv[$oi] -ne '--expected-owner-account') { $oi++ }
Assert-Equal $wrapArgv[$oi + 1] ([string]$entryConfig['share_account']) 'the wrapper argv carries the SHARE account'
$si = 0; while ($wrapArgv[$si] -ne '--expect-owner-sid') { $si++ }
Assert-Equal $wrapArgv[$si + 1] ([string]$entryConfig['share_sid']) 'the wrapper argv carries the SHARE SID (the broker refuses to install without it)'
Assert-True (-not ($wrapArgv -contains ([string]$entryConfig['operator_account']))) 'the OPERATOR account never appears in the broker launch argv'
$shDef = [xml]$schI.State['Tasks']['umshare-share-exp']
Assert-Equal ([string]$shDef.Task.Principals.Principal.UserId) ([string]$entryConfig['share_sid']) 'the share task principal is the COLLECTED share SID'

Write-Host '== R78-C: the REAL install manifest decides the ledger owned set (division) =='
# The SAME run's ledger: the manifest's created+hash entries are imported;
# the 'shared' pre-existing artifact and the install's runtime artifact stay
# OUT of this run's owned set.
$ledgerI = Invoke-D4File -Boundary $osI.Boundary -Operation 'ReadText' -Arguments @(
    (Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId)) | ConvertFrom-Json
$ownedPaths = @($ledgerI.entries | Where-Object { [string]$_.status -eq 'created' } | ForEach-Object { [string]$_.path.ToLowerInvariant() })
Assert-True ($ownedPaths -contains ((Join-Path $contract['ExperimentRoot'] 'broker\hook-fixture.py').ToLowerInvariant())) 'the manifest created entry IS in the ledger (with its hash)'
$hookFixEntry = @($ledgerI.entries | Where-Object { $_.path -like '*hook-fixture.py' })[0]
Assert-Equal ([string]$hookFixEntry.sha256_actual) ((Invoke-D4File -Boundary $osI.Boundary -Operation 'Hash' -Arguments @((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'hook-fixture.py')))) 'the imported hash equals the ACTUAL file content hash'
Assert-True (-not (($ownedPaths -join ';') -match 'pre-existing\.dll$')) 'the SHARED pre-existing artifact stays OUT of the ledger'
Assert-True (-not (($ownedPaths -join ';') -match 'broker-status\.json$')) 'the install runtime artifact stays OUT of the ledger'
$installStateEntry = @($ledgerI.entries | Where-Object { $_.path -like '*install-state.json' })[0]
Assert-Equal ([string]$installStateEntry.sha256_actual) ((Invoke-D4File -Boundary $osI.Boundary -Operation 'Hash' -Arguments @((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'install-state.json')))) 'install-state.json is frozen with its ACTUAL hash'

Write-Host '== R78-C: a bad ledger tail refuses the WHOLE cleanup with ZERO operations =='
$osZ = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schZ = New-D4FakeSchtasks -Oses $osZ
$aclZ = New-D4HarnessAclRunner -Oses $osZ
$storeZ = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splatZ = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schZ.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeZ.Runner -Os $osZ -Acl $aclZ.Runner `
    -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatZ | Out-Null
& $entry -Stage 'S1' @splatZ | Out-Null
$ledgerZPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledgerZ = Invoke-D4File -Boundary $osZ.Boundary -Operation 'ReadText' -Arguments @($ledgerZPath) | ConvertFrom-Json
$badEntry = @{ path = 'C:\Windows\System32\evil.txt'; kind = 'mystery'; status = 'created'; sha256_actual = '' }
$ledgerZ.entries = @($ledgerZ.entries) + @($badEntry)
Invoke-D4File -Boundary $osZ.Boundary -Operation 'WriteText' -Arguments @(
    $ledgerZPath, ($ledgerZ | ConvertTo-Json -Depth 6))
$preEnd = (@($schZ.State['Calls']) | Where-Object { $_ -match '/End' }).Count
$preDel = (@($schZ.State['Calls']) | Where-Object { $_ -match '/Delete' }).Count
$preRem = (@($storeZ.State['Calls']) | Where-Object { $_ -match 'limits remove' }).Count
Assert-Throws { & $entry -Stage 'Cleanup' @splatZ } 'a bad ledger record refuses the WHOLE cleanup' 'ledger pre-validation FAILED'
Assert-Equal ((@($schZ.State['Calls']) | Where-Object { $_ -match '/End' }).Count) $preEnd 'the refused cleanup performed ZERO task /End'
Assert-Equal ((@($schZ.State['Calls']) | Where-Object { $_ -match '/Delete' }).Count) $preDel 'the refused cleanup performed ZERO task /Delete'
Assert-Equal ((@($storeZ.State['Calls']) | Where-Object { $_ -match 'limits remove' }).Count) $preRem 'the refused cleanup performed ZERO rule removals'
Assert-True ($osZ.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'the owned files are UNTOUCHED (nothing deleted)'
Assert-True ($osZ.State['Files'].ContainsKey((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'hook-fixture.py').ToLowerInvariant())) 'the install created objects are UNTOUCHED'
Assert-Equal ($storeZ.State['Live'].Count) 3 'the limit rules are UNTOUCHED (zero removals)'

Write-Host '== R78-C: the same run recovers and completes the cleanup =='
$ledgerZ = Invoke-D4File -Boundary $osZ.Boundary -Operation 'ReadText' -Arguments @($ledgerZPath) | ConvertFrom-Json
$fixedEntries = @($ledgerZ.entries | Where-Object { $_.kind -ne 'mystery' })
$ledgerZ.entries = $fixedEntries
Invoke-D4File -Boundary $osZ.Boundary -Operation 'WriteText' -Arguments @(
    $ledgerZPath, ($ledgerZ | ConvertTo-Json -Depth 6))
& $entry -Stage 'Cleanup' @splatZ | Out-Null
Assert-Equal $LASTEXITCODE 0 'the SAME run recovers and completes the cleanup'
Assert-True (-not $osZ.State['Files'].ContainsKey(($contract['ExperimentRoot'] + '\deploy-mode.json').ToLowerInvariant())) 'the owned files are GONE after the recovered cleanup'
Assert-True (-not $osZ.State['Files'].ContainsKey((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'hook-fixture.py').ToLowerInvariant())) 'the install created objects are GONE after the recovered cleanup'
Assert-Equal ($storeZ.State['Live'].Count) 0 'the limit rules are ABSENT after the recovered cleanup'

Write-Host '== R78-D: the config validates the rule SET as a whole (negatives) =='
# The reviewed isolation TRIPLE is validated as a set: a missing block-v4 or
# block-v6 (a partial set can never prove default-deny outbound), a
# duplicate id (roles otherwise valid), an extra rule, a wrong role set, an
# inconsistent image and a non-experiment image ALL refuse - zero starts.
function Copy-D4TestConfig {
    param([object[]]$LimitRules)
    $c = @{} ; foreach ($k in $entryConfig.Keys) { $c[$k] = $entryConfig[$k] }
    $c['limit_rules'] = $LimitRules
    return $c
}
$cfgMissB4 = Copy-D4TestConfig @($rulePermit, $ruleBlockV6)
Assert-Throws { Test-D4ExperimentConfig -Config $cfgMissB4 -Contract $contract } 'a missing block-v4 refuses the config' 'EXACTLY the reviewed isolation triple'
$cfgMissB6 = Copy-D4TestConfig @($rulePermit, $ruleBlockV4)
Assert-Throws { Test-D4ExperimentConfig -Config $cfgMissB6 -Contract $contract } 'a missing block-v6 refuses the config' 'EXACTLY the reviewed isolation triple'
$cfgDupId = Copy-D4TestConfig @($rulePermit, $ruleBlockV4,
    @{ id = $ruleBlockV4['id']; layer = 'connect-v6'; action = 'block'; app_path = $ruleBlockV6['app_path'] })
Assert-Throws { Test-D4ExperimentConfig -Config $cfgDupId -Contract $contract } 'a duplicate rule id refuses (roles otherwise valid)' 'DUPLICATE rule id'
$cfgExtra = Copy-D4TestConfig @($rulePermit, $ruleBlockV4, $ruleBlockV6,
    @{ id = 'c3d6f8a9-4e5b-4c70-8d92-3a1b4c5d6e88'; layer = 'connect-v4'; action = 'block'; app_path = $ruleBlockV4['app_path'] })
Assert-Throws { Test-D4ExperimentConfig -Config $cfgExtra -Contract $contract } 'an extra rule refuses (not the reviewed triple)' 'EXACTLY the reviewed isolation triple'
$cfgWrongRoles = Copy-D4TestConfig @($ruleBlockV4,
    @{ id = 'd4e7a9b0-5f6c-4d81-9e03-4b2c5d6e7f99'; layer = 'connect-v4'; action = 'block'; app_path = $ruleBlockV4['app_path'] }, $ruleBlockV6)
Assert-Throws { Test-D4ExperimentConfig -Config $cfgWrongRoles -Contract $contract } 'a no-permit triple refuses (the role set is validated)' 'EXACTLY one connect-v4/permit'
$cfgMixedImage = Copy-D4TestConfig @($rulePermit,
    @{ id = $ruleBlockV4['id']; layer = 'connect-v4'; action = 'block'; app_path = 'C:\other\image.exe' }, $ruleBlockV6)
Assert-Throws { Test-D4ExperimentConfig -Config $cfgMixedImage -Contract $contract } 'an inconsistent image across rules refuses' 'ONE consistent image'
$cfgForeignImage = Copy-D4TestConfig @(
    @{ id = $rulePermit['id']; layer = 'connect-v4'; action = 'permit';
       app_path = 'C:\ProgramData\UMShareExp-broker\other.exe';
       remote_addr = '127.0.0.1'; remote_port = 9100 },
    @{ id = $ruleBlockV4['id']; layer = 'connect-v4'; action = 'block';
       app_path = 'C:\ProgramData\UMShareExp-broker\other.exe' },
    @{ id = $ruleBlockV6['id']; layer = 'connect-v6'; action = 'block';
       app_path = 'C:\ProgramData\UMShareExp-broker\other.exe' })
Assert-Throws { Test-D4ExperimentConfig -Config $cfgForeignImage -Contract $contract } 'a non-experiment image refuses' 'FIXED experiment image'
$osD = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schD2 = New-D4FakeSchtasks -Oses $osD
$aclD2 = New-D4HarnessAclRunner -Oses $osD
$storeD2 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$cfgDPath = Join-Path $tmpBase 'entry-config-r78d.json'
$cfgD = Copy-D4TestConfig @($rulePermit, $ruleBlockV6)
[IO.File]::WriteAllText($cfgDPath, ($cfgD | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$splatD2 = New-D4EntrySplat -RunId $runId -ConfigPath $cfgDPath -Schtasks $schD2.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeD2.Runner -Os $osD -Acl $aclD2.Runner `
    -Installer $brokerInstaller -Store $storeFactory
$preWritesD = @($osD.State['Writes']).Count
$preRunsD = (@($schD2.State['Calls']) | Where-Object { $_ -match '/Run' }).Count
Assert-Throws { & $entry -Stage 'S0' @splatD2 } 'an incomplete rule set refuses AT CONFIG LOAD (zero starts)' 'EXACTLY the reviewed isolation triple'
Assert-Equal ((@($schD2.State['Calls']) | Where-Object { $_ -match '/Run' }).Count) $preRunsD 'the refused config started NOTHING'
Assert-Equal (@($osD.State['Writes']).Count) $preWritesD 'the refused config wrote ZERO objects'

Write-Host '== R78-C: a HALF-completed install is recorded on the SAME run and the run recovers =='
# The fixture mirrors the REAL install's PROGRESSIVE manifest (every
# artifact is written and recorded created+ACTUAL-hash BEFORE the next one)
# and then FAILS mid-install. The S1 abort must import the already-created
# entries (and the manifest itself) into THIS run's intent ledger, so the
# same-run cleanup can finish exactly these objects by identity - never a
# post-return discovery, never a success claim.
$osH = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schH = New-D4FakeSchtasks -Oses $osH
$aclH = New-D4HarnessAclRunner -Oses $osH
$storeH = New-D4FakeRuleStore -CreatedFacts $createdFacts
$halfInstaller = {
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [scriptblock]$IcaclsRunner, [hashtable]$Boundary)
    Invoke-D4File -Boundary $Boundary -Operation 'MakeDir' -Arguments @(
        (Join-Path $Cfg['RunRoot'] 'broker'))
    $mods = @()
    foreach ($rel in @('broker\hook-fixture.py', 'broker\broker-control-key')) {
        $dst = Join-Path $Cfg['RunRoot'] $rel
        Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
            $dst, ('HOOK-FIXTURE ' + $rel))
        $mods += @{ file = $rel;
                    sha256 = (Invoke-D4File -Boundary $Boundary -Operation 'Hash' -Arguments @($dst));
                    classification = 'created' }
        # Progressive record: the manifest is rewritten after EVERY artifact
        # (a crash between write and record never leaves an unrecorded
        # created artifact behind - the real install's R51.3 contract).
        Invoke-D4File -Boundary $Boundary -Operation 'WriteText' -Arguments @(
            (Join-Path (Join-Path $Cfg['RunRoot'] 'broker') 'install-state.json'),
            (@{ task_name = $Cfg['TaskName']; modules = $mods } | ConvertTo-Json -Depth 4))
    }
    throw 'injected half-install failure (after two recorded artifacts)'
}.GetNewClosure()
$splatH = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schH.Runner `
    -Net $net.Runner -Proc $proc.Runner -Rules $storeH.Runner -Os $osH -Acl $aclH.Runner `
    -Installer $halfInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatH | Out-Null
Assert-Throws { & $entry -Stage 'S1' @splatH } 'the half-completed install fails S1 (never a success claim)' 'injected half-install failure'
$ledgerHPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledgerH = Invoke-D4File -Boundary $osH.Boundary -Operation 'ReadText' -Arguments @($ledgerHPath) | ConvertFrom-Json
foreach ($rel in @('broker\hook-fixture.py', 'broker\broker-control-key')) {
    $entryH = @($ledgerH.entries | Where-Object { $_.path -like ('*\' + $rel) })[0]
    Assert-Equal ([string]$entryH.status) 'created' ('the half-install artifact ' + $rel + ' is recorded on the SAME run')
    Assert-Equal ([string]$entryH.sha256_actual) ((Invoke-D4File -Boundary $osH.Boundary -Operation 'Hash' -Arguments @((Join-Path $contract['ExperimentRoot'] $rel)))) ('the recorded hash of ' + $rel + ' is the ACTUAL content hash')
}
$manifestHEntry = @($ledgerH.entries | Where-Object { $_.path -like '*install-state.json' })[0]
Assert-Equal ([string]$manifestHEntry.status) 'created' 'the install manifest itself is claimed by the abort path'
$stateHPath = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
$stateH = Invoke-D4File -Boundary $osH.Boundary -Operation 'ReadText' -Arguments @($stateHPath) | ConvertFrom-Json
Assert-Equal ([string]$stateH.stages.S1.verdict) 'fail' 'the stage state records the S1 failure truthfully'
Assert-True ([string]$stateH.stages.S1.detail -match 'hook-fixture') 'the fail detail lists the imported created objects as unresolved'
# Same-run recovery: the cleanup resolves EVERY recorded object by identity.
& $entry -Stage 'Cleanup' @splatH | Out-Null
Assert-Equal $LASTEXITCODE 0 'the SAME run recovers: cleanup completes over the half-install evidence'
Assert-True (-not $osH.State['Files'].ContainsKey((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'hook-fixture.py').ToLowerInvariant())) 'the half-install artifacts are GONE after the recovered cleanup'
Assert-True (-not $osH.State['Files'].ContainsKey((Join-Path (Join-Path $contract['ExperimentRoot'] 'broker') 'install-state.json').ToLowerInvariant())) 'the install manifest is GONE after the recovered cleanup'

Write-Host '== R78-D: the REAL runner decision loop driven over a fake NATIVE surface =='
# Only the OS calls are replaced: the reviewed spec/id gate, the parameter
# conversion, the REAL codec encode/decode and the ownership classification
# run unmodified. The chain walks permit AND block through
# create -> verify -> existing -> remove -> ABSENT (no pre-canned facts).
$native = New-D4FakeNativeOps
$runnerR = New-D4RealRulesRunner -RuleSpecs $ruleSpecs -Contract $contract -NativeOps $native.Ops
$allocBefore = [UmD4RuleCodec]::LiveAllocations
$ids3 = @($rulePermit['id'], $ruleBlockV4['id'], $ruleBlockV6['id'])
$rC = Invoke-CommandRunner -Runner $runnerR -ArgumentList (@('limits', 'create') + $ids3)
Assert-Equal $rC.ExitCode 0 'create: the reviewed triple is CREATED through the REAL decision code'
Assert-True ((@($rC.Output) | Where-Object { $_ -like 'CREATED *' }).Count -eq 3) 'three CREATED verdicts (permit scope + both block paths)'
$rV = Invoke-CommandRunner -Runner $runnerR -ArgumentList (@('limits', 'verify') + $ids3)
Assert-Equal $rV.ExitCode 0 'verify: all three rules PRESENT'
Assert-True ((@($rV.Output) | Where-Object { $_ -like 'PRESENT * appid=MATCH*' }).Count -eq 3) 'the stored images decode to appid MATCH through the REAL codec'
# NOTE: the @() around the Where-Object RESULT is required - a single match
# returns a SCALAR and [0] would index its first character.
$permitLine = @(@($rV.Output) | Where-Object { $_ -like ('PRESENT ' + $rulePermit['id'] + ' *') })[0]
Assert-True ($permitLine -match ('port=' + [string]$contract['SharePort'] + '\s*$')) 'the permit image carries the CONTRACT share port (the Contract reaches the facts comparison)'
Assert-True ($permitLine -match 'addr=2130706433') 'the permit image carries the spec scope address'
$rE = Invoke-CommandRunner -Runner $runnerR -ArgumentList (@('limits', 'create') + $ids3)
Assert-Equal $rE.ExitCode 0 're-create over matching images is idempotent'
Assert-True ((@($rE.Output) | Where-Object { $_ -like 'ALREADY-PRESENT*' }).Count -eq 3) 'three ALREADY-PRESENT verdicts from the REAL facts comparison'
$rR = Invoke-CommandRunner -Runner $runnerR -ArgumentList (@('limits', 'remove') + $ids3)
Assert-Equal $rR.ExitCode 0 'remove: identity-verified removal of the triple'
Assert-True ((@($rR.Output) | Where-Object { $_ -like 'REMOVED *' }).Count -eq 3) 'three REMOVED verdicts'
$rA = Invoke-CommandRunner -Runner $runnerR -ArgumentList (@('limits', 'list') + $ids3)
Assert-Equal $rA.ExitCode 0 'list after removal exits 0'
Assert-True ((@($rA.Output) | Where-Object { $_ -like 'ABSENT *' }).Count -eq 3) 'after removal every id reports ABSENT (closed loop)'
# A same-key FOREIGN image (different app-id bytes) refuses BOTH takeover paths.
$foreignOps = New-D4FakeNativeOps
$foreignKey = [Guid]::Parse($rulePermit['id'])
$foreignFilter = [UmD4RuleCodec+NativeFilter]::new(
    $foreignKey, 'um-share-exp-foreign', [Guid]$script:D4WfpLayerGuids['connect-v4'],
    [UmD4RuleCodec]::ACT_PERMIT, [Text.Encoding]::Unicode.GetBytes('C:\foreign\image.exe'),
    $true, 2130706433, [uint16]$contract['SharePort'], [Guid]$script:D4WfpSubLayer)
# Seed THROUGH the fake's own Add (the deep-copy path the runner's create
# uses) - a hand-copied image would leave the cond/blob pointers dangling.
$seedId = [uint64]0
$null = & $foreignOps.Ops['Add'] ([IntPtr]24) $foreignFilter ([ref]$seedId)
$foreignFilter.Dispose()
$runnerF = New-D4RealRulesRunner -RuleSpecs $ruleSpecs -Contract $contract -NativeOps $foreignOps.Ops
$rFC = Invoke-CommandRunner -Runner $runnerF -ArgumentList @('limits', 'create', $rulePermit['id'])
Assert-Equal $rFC.ExitCode 3 'a FOREIGN same-key image refuses the create takeover (REAL facts mismatch)'
Assert-True ((@($rFC.Output) -join "`n") -match '^FOREIGN ') 'the refusal names the foreign facts mismatch'
$rFR = Invoke-CommandRunner -Runner $runnerF -ArgumentList @('limits', 'remove', $rulePermit['id'])
Assert-Equal $rFR.ExitCode 3 'a FOREIGN same-key image refuses the identity-checked removal'
Assert-True $foreignOps.State['Store'].ContainsKey($foreignKey.ToString().ToLowerInvariant()) 'the foreign image stays UNTOUCHED by the refused remove'
# A HALF-injected native surface refuses at construction.
Assert-Throws { New-D4RealRulesRunner -RuleSpecs $ruleSpecs -Contract $contract -NativeOps @{ EngineOpen = {} } } 'an incomplete native surface refuses (half-injection never passes silently)' 'incomplete'
Assert-Equal ([UmD4RuleCodec]::LiveAllocations) $allocBefore 'the native fixtures leave ZERO live codec allocations (dispose balance)'

Write-Host '== R80: a genuinely OCCUPIED 9100 port refuses S0 (zero writes) =='
# The stateful lifecycle pair never hides a REAL occupation: an unrelated
# live listener on 9100 (independent of this run's schtasks) must make the
# S0 namespace-free gate refuse with zero writes - never accepted, never
# attributed to this run.
$osOcc = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schOcc = New-D4FakeSchtasks -Oses $osOcc
$aclOcc = New-D4HarnessAclRunner -Oses $osOcc
$storeOcc = New-D4FakeRuleStore -CreatedFacts $createdFacts
$splatOcc = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $schOcc.Runner `
    -Net (New-D4FakeOccupiedPort) -Proc (New-D4EmptyRunner -Tag 'proc').Runner `
    -Rules $storeOcc.Runner -Os $osOcc -Acl $aclOcc.Runner -Installer $brokerInstaller -Store $storeFactory
$writesOcc = @($osOcc.State['Writes']).Count
Assert-Throws { & $entry -Stage 'S0' @splatOcc } 'S0 refuses a genuinely OCCUPIED port (real occupation is never ignored)' 'port 9100 has a listener'
Assert-Equal (@($osOcc.State['Writes']).Count) $writesOcc 'the occupied-port S0 wrote ZERO records'

Write-Host '== R83: retention counter-examples - no deletion right, no writer claim, no ownership overwrite =='
# (a) PLANNED-but-not-created path with a stable FOREIGN file: the writing
# stage MUST NOT upgrade the entry to created / grant a deletion right on
# re-computed hashes - the file is retained with a per-object reason, the
# recorded ownership stays untouched and no removal ever happens.
$osR = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$ledgerRPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledgerR = New-D4IntentLedger -Inventory $inventory -LedgerPath $ledgerRPath -FileBoundary $osR.Boundary
$storePathR = Join-Path $contract['ExperimentRoot'] 'share-store.db'
$storeKeyR = $storePathR.ToLowerInvariant()
$osR.State['Files'][$storeKeyR] = 'FOREIGN-STABLE-STORE'   # direct injection: bypasses this run's write log
$ledgerR = Save-D4DynamicRetention -LedgerPath $ledgerRPath -PackageHash $pkg -RunId $runId `
    -ExperimentRoot $contract['ExperimentRoot'] -RelativePaths @('share-store.db') `
    -Retention 'foreign/unknown bytes at a planned path; not this run ownable offline; retained, never deleted' `
    -FileBoundary $osR.Boundary
$storeR = @($ledgerR.entries | Where-Object { $_.path -like '*share-store.db' })[0]
Assert-True ([string]$storeR.status -eq 'retained') 'a stable foreign file at a PLANNED path is RETAINED, never upgraded to created'
Assert-Equal ([string]$storeR.sha256_actual) '' 'the retained entry does NOT adopt a foreign hash (no ownership claim by re-hash)'
Assert-True (-not $osR.State['Removes'].Contains($storePathR)) 'the retention stage never removes the foreign bytes'
Assert-Equal ([string]$osR.State['Files'][$storeKeyR]) 'FOREIGN-STABLE-STORE' 'the foreign bytes are byte-preserved'
# (a2) a RETAINED object whose file VANISHED after retention is reported by
# the final verdict - never silently passed as clean.
$osR.State['Files'].Remove($storeKeyR)
$missingRetained = @(Test-D4CleanupComplete -Inventory $inventory -Contract $contract -Ledger $ledgerR `
    -EvidenceDir (Join-Path $contract['ExperimentRoot'] 'evidence') `
    -SchtasksRunner $absentTask -ProcessEvidence @{ Unconfirmed = $false; Pids = @() } `
    -PortEvidence @{ Unconfirmed = $false; Listening = $false } `
    -FileBoundary $osR.Boundary -RulesRunner $emptyRules.Runner)
Assert-True (($missingRetained -join ';') -match 'retained object is MISSING') 'a RETAINED object whose file vanished is reported (never claimed clean)'
# (b) a CREATED object replaced by stable foreign bytes BEFORE the stage
# records it: the existing creation hash is NOT overwritten and no deletion
# right is granted for the foreign content.
$projRelR = [string]$contract['ProjectionRel']
$projPathR = Join-Path $contract['ExperimentRoot'] $projRelR
$projKeyR = $projPathR.ToLowerInvariant()
$osR.State['Files'][$projKeyR] = '{"published": 1, "generation": 1}'     # S1's genuine creation bytes
$ledgerR = Update-D4IntentLedger -Ledger $ledgerR -LedgerPath $ledgerRPath -Path $projPathR `
    -Status 'created' -Sha256 (Invoke-D4File -Boundary $osR.Boundary -Operation 'Hash' -Arguments @($projPathR)) `
    -FileBoundary $osR.Boundary
$projCreationR = [string](@($ledgerR.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0].sha256_actual)
$osR.State['Files'][$projKeyR] = '{"foreign": true}'                      # stable replacement BEFORE the record
$ledgerR = Save-D4DynamicRetention -LedgerPath $ledgerRPath -PackageHash $pkg -RunId $runId `
    -ExperimentRoot $contract['ExperimentRoot'] -RelativePaths @($projRelR) `
    -Retention 'created-then-replaced before record; final state not provable; retained, never deleted' `
    -FileBoundary $osR.Boundary
$projR = @($ledgerR.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0]
Assert-True ([string]$projR.status -eq 'retained') 'a created projection replaced before the record is RETAINED (never granted a deletion right)'
Assert-Equal ([string]$projR.sha256_actual) $projCreationR 'the existing creation hash is NOT overwritten by foreign bytes'
Assert-Equal ([string]$osR.State['Files'][$projKeyR]) '{"foreign": true}' 'the foreign replacement bytes stay untouched'
# (c) a STILL-ALIVE writer (live process evidence) that is silent across any
# two samples: the record must NEVER claim "writer stopped" / write a
# freezer stamp - retention is recorded without a stop claim.
$pairR = New-D4LifecycleNetPair -SchRunner (New-D4FakeSchtasks -Oses $osR).Runner -ExperimentRoot $contract['ExperimentRoot']
$null = & $pairR.Schtasks @('schtasks', '/Run', '/TN', $contract['BrokerTaskName'])
$null = & $pairR.Proc @('pwsh', '-NoProfile', '-Command', 'x')
$ledgerR = Save-D4DynamicRetention -LedgerPath $ledgerRPath -PackageHash $pkg -RunId $runId `
    -ExperimentRoot $contract['ExperimentRoot'] -RelativePaths @($projRelR) `
    -Retention 'writer still alive (live process evidence); final state unconfirmed; retained, never deleted' `
    -FileBoundary $osR.Boundary
$projR2 = @($ledgerR.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0]
Assert-True ((-not ([string]$projR2.retained_reason -match 'stopped')) -and
    -not (([string]$projR2.PSObject.Properties.Name -join ',') -match 'dynamic_final')) 'the retention record never claims the writer stopped'
Assert-True ([string]$projR2.status -eq 'retained') 'a live-but-silent writer still yields the retained terminal (never a freeze claim)'

Write-Host '== R83: the FULL lifecycle retains the dynamic artifacts and reports service-closed separately =='
# The fake probe ACTUALLY WRITES what it stands for (a success-text-only
# probe is not lifecycle evidence): E3 rewrites the projection twice and
# creates the isolated store; E4 changes both again. Offline the rewritten
# files' final state is not provable, so the writing stages only mark them
# RETAINED (per-object reason); the cleanup then verifies the SERVICES are
# closed (no task/process/listener/rule) and reports the kept files - it
# never deletes a retained dynamic artifact and never adopts a current hash.
$osF = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schF = New-D4FakeSchtasks -Oses $osF
$aclF = New-D4HarnessAclRunner -Oses $osF
$storeF = New-D4FakeRuleStore -CreatedFacts $createdFacts
$probeF = New-D4FakeProbe -Oses $osF -Contract $contract
$pairF = New-D4LifecycleNetPair -SchRunner $schF.Runner -ExperimentRoot $contract['ExperimentRoot']
$splatF = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pairF.Schtasks `
    -Net $pairF.Net -Proc $pairF.Proc -Probe $probeF.Runner `
    -Rules $storeF.Runner -Os $osF -Acl $aclF.Runner -Installer $brokerInstaller -Store $storeFactory
# E4 harness needs an isolated REAL scenario root carrying this experiment's own install (reentry base).
$scenRootF = Join-Path $tmpBase 'scen-root-full'
New-D4ScenarioRunRoot -Dir $scenRootF
$scenCfgF = New-D4ScenarioCfgForEntry -RunRoot $scenRootF -Contract $contract -Config $config
$scenSchF = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
Install-Broker -Cfg $scenCfgF -SchtasksRunner $scenSchF.Runner -IcaclsRunner (New-D4GoodIcacls)
$scenSchFailF = New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }
$scenSchFailF.State['CreateOk'] = $false
$splatF4 = @{}; foreach ($k in $splatF.Keys) { $splatF4[$k] = $splatF[$k] }
$splatF4['ScenarioRoot'] = $scenRootF
$splatF4['IcaclsRunner'] = (New-D4GoodIcacls)
$splatF4['FailingIcaclsRunner'] = (New-D4FailingSetIcacls)
$splatF4['FailingSchtasksRunner'] = $scenSchFailF.Runner
$splatF4['SchtasksRunner'] = (New-D4FakeSchtasks -Oses @{ Boundary = (New-D4RealFileBoundary) }).Runner
$ledgerFPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
foreach ($s in @('S0', 'S1')) {
    & $entry -Stage $s @splatF | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('stage ' + $s + ' exits 0')
}
$ledgerF = Invoke-D4File -Boundary $osF.Boundary -Operation 'ReadText' -Arguments @($ledgerFPath) | ConvertFrom-Json
$projFCreation = [string](@($ledgerF.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0].sha256_actual)
foreach ($s in @('E1', 'E2', 'E3')) {
    & $entry -Stage $s @splatF | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('stage ' + $s + ' exits 0')
}
$ledgerF = Invoke-D4File -Boundary $osF.Boundary -Operation 'ReadText' -Arguments @($ledgerFPath) | ConvertFrom-Json
$projE3 = @($ledgerF.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0]
Assert-True ([string]$projE3.status -eq 'retained') 'E3 marks the written projection RETAINED (no deletion right)'
Assert-True ([string]$projE3.retained_reason -like '*E3*') 'the projection retention reason records the E3 writing stage'
Assert-Equal ([string]$projE3.sha256_actual) $projFCreation 'the projection CREATION hash is preserved (never replaced by the current bytes)'
$storeE3 = @($ledgerF.entries | Where-Object { $_.path -like '*share-store.db' })[0]
Assert-True ([string]$storeE3.status -eq 'retained') 'the isolated store is RETAINED after E3 (planned + fresh bytes never upgrade to created)'
& $entry -Stage 'E4' @splatF4 | Out-Null
Assert-Equal $LASTEXITCODE 0 'stage E4 exits 0 (four fault boundaries + harness probe write)'
$ledgerF = Invoke-D4File -Boundary $osF.Boundary -Operation 'ReadText' -Arguments @($ledgerFPath) | ConvertFrom-Json
$projE4 = @($ledgerF.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0]
Assert-True ([string]$projE4.status -eq 'retained') 'E4 keeps the projection RETAINED'
Assert-True ([string]$projE4.retained_reason -like '*E4*') 'the projection retention reason records the E4 writing stage'
Assert-Equal ([string]$projE4.sha256_actual) $projFCreation 'E4 does not overwrite the creation hash'
$cleanupFOut = @(& $entry -Stage 'Cleanup' @splatF 2>&1)
Assert-Equal $LASTEXITCODE 0 'the FULL lifecycle cleanup completes: services closed, dynamic artifacts retained + reported'
Assert-True (($cleanupFOut -join "`n") -match 'no experiment task/process/listener/rule') 'the cleanup reports the SERVICES closed'
Assert-True (($cleanupFOut -join "`n") -match 'retained') 'the cleanup output reports the retained dynamic artifacts'
Assert-True ($osF.State['Files'].ContainsKey((Join-Path $contract['ExperimentRoot'] $contract['ProjectionRel']).ToLowerInvariant())) 'the projection is KEPT (retained, never deleted)'
Assert-True ($osF.State['Files'].ContainsKey((Join-Path $contract['ExperimentRoot'] 'share-store.db').ToLowerInvariant())) 'the isolated store is KEPT (retained, never deleted)'

Write-Host '== R83: a retention-record INTERRUPTION is recovered in the SAME run =='
$osH = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schH = New-D4FakeSchtasks -Oses $osH
$aclH = New-D4HarnessAclRunner -Oses $osH
$storeH = New-D4FakeRuleStore -CreatedFacts $createdFacts
$probeH = New-D4FakeProbe -Oses $osH -Contract $contract -FailAfterWrites
$pairH = New-D4LifecycleNetPair -SchRunner $schH.Runner -ExperimentRoot $contract['ExperimentRoot']
$splatH = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pairH.Schtasks `
    -Net $pairH.Net -Proc $pairH.Proc -Probe $probeH.Runner `
    -Rules $storeH.Runner -Os $osH -Acl $aclH.Runner -Installer $brokerInstaller -Store $storeFactory
$ledgerHPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
foreach ($s in @('S0', 'S1', 'E1', 'E2')) {
    & $entry -Stage $s @splatH | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('interrupted-run stage ' + $s + ' exits 0')
}
Assert-Throws { & $entry -Stage 'E3' @splatH } 'the mid-lifecycle failure fails E3 (after the publications landed)' 'projection/rotation closure'
$ledgerH = Invoke-D4File -Boundary $osH.Boundary -Operation 'ReadText' -Arguments @($ledgerHPath) | ConvertFrom-Json
$projH = @($ledgerH.entries | Where-Object { $_.path -like '*um-share-projection.json' })[0]
Assert-True ([string]$projH.status -eq 'retained') 'the aborted E3 marks the projection RETAINED'
Assert-True ([string]$projH.retained_reason -like '*E3 aborted*') 'the abort retention reason is recorded on the entry'
$storeH2 = @($ledgerH.entries | Where-Object { $_.path -like '*share-store.db' })[0]
Assert-True ([string]$storeH2.status -eq 'retained') 'the aborted E3 retains the store (no created upgrade)'
$stateH = Get-D4StageStatePath -Contract $contract -PackageHash $pkg -RunId $runId
$stateHObj = Invoke-D4File -Boundary $osH.Boundary -Operation 'ReadText' -Arguments @($stateH) | ConvertFrom-Json
Assert-Equal ([string]$stateHObj.stages.E3.verdict) 'fail' 'the stage state records the E3 failure truthfully'
Assert-Throws { & $entry -Stage 'E4' @splatH } 'E4 is blocked after the failed E3 (disorder refusal)' 'not recorded as pass'
& $entry -Stage 'Cleanup' @splatH | Out-Null
Assert-Equal $LASTEXITCODE 0 'the SAME run recovers: cleanup closes over the retention record'
Assert-True ($osH.State['Files'].ContainsKey((Join-Path $contract['ExperimentRoot'] $contract['ProjectionRel']).ToLowerInvariant())) 'the projection is KEPT after the recovered cleanup'
Assert-True ($osH.State['Files'].ContainsKey((Join-Path $contract['ExperimentRoot'] 'share-store.db').ToLowerInvariant())) 'the store is KEPT after the recovered cleanup'

Write-Host '== R85: a retained file keeps ONLY its parent container retained; unknown/failed content is never masked =='
# REAL-mode E4 adds broker\install-state.json to the retention set (the
# harness scenario root does not). The broker dir then holds a legally
# retained child the hash-gated deletion deliberately skips, so the dir must
# be retained + reported itself - NEVER failed-clean - and only when the
# ACTUAL remaining children are all legal retained ledger objects. Unknown
# content or failed entries in the same container keep the strict semantics.
$os85 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch85 = New-D4FakeSchtasks -Oses $os85
$acl85 = New-D4HarnessAclRunner -Oses $os85
$rules85 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$pair85 = New-D4LifecycleNetPair -SchRunner $sch85.Runner -ExperimentRoot $contract['ExperimentRoot']
$splat85 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pair85.Schtasks `
    -Net $pair85.Net -Proc $pair85.Proc `
    -Rules $rules85.Runner -Os $os85 -Acl $acl85.Runner -Installer $brokerInstaller -Store $storeFactory
$ledger85Path = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
foreach ($s in @('S0', 'S1')) {
    & $entry -Stage $s @splat85 | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('R85 stage ' + $s + ' exits 0')
}
# the REAL-mode retained child: broker\install-state.json (the exact relative
# path the real-mode E4 evidenceRels adds), marked through the same
# retention function the writing stage calls.
$ledger85 = Save-D4DynamicRetention -LedgerPath $ledger85Path -PackageHash $pkg -RunId $runId `
    -ExperimentRoot $contract['ExperimentRoot'] -RelativePaths @('broker\install-state.json') `
    -Retention 'E4 fault matrix (real mode): offline final-state evidence not provable; retained, never granted deletion' `
    -FileBoundary $os85.Boundary
$cleanup85A = @(& $entry -Stage 'Cleanup' @splat85 2>&1)
Assert-Equal $LASTEXITCODE 0 'Group A: with ONLY a legal retained child the broker container does NOT block'
Assert-True ($os85.State['Files'].ContainsKey((Join-Path $contract['ExperimentRoot'] 'broker\install-state.json').ToLowerInvariant())) 'the retained install manifest is KEPT'
Assert-True ($os85.State['Dirs'].ContainsKey((Join-Path $contract['ExperimentRoot'] 'broker').ToLowerInvariant())) 'the broker container is KEPT'
$ledger85 = Invoke-D4File -Boundary $os85.Boundary -Operation 'ReadText' -Arguments @($ledger85Path) | ConvertFrom-Json
$brokerDir85 = @($ledger85.entries | Where-Object { $_.path -like '*\broker' -and $_.kind -eq 'dir' })[0]
Assert-True ([string]$brokerDir85.status -eq 'retained') 'the broker container is itself RETAINED (never failed-clean)'
Assert-True ([string]$brokerDir85.retained_reason -like '*parent container*') 'the container retention reason is recorded'
Assert-True (($cleanup85A -join "`n") -match 'RETAINED dynamic artifacts') 'the retained container appears in the retained report'
# Group A2: the same-run re-cleanup is idempotent over the retained container.
$cleanup85B = @(& $entry -Stage 'Cleanup' @splat85 2>&1)
Assert-Equal $LASTEXITCODE 0 'Group A2: the same-run re-cleanup stays idempotent over the retained container'
# Group B: an UNKNOWN file in the SAME container is NOT exempted by the
# retained child - the container keeps its strict failed-clean semantics
# and the unknown bytes are preserved and reported.
$os85.State['Files'][(Join-Path $contract['ExperimentRoot'] 'broker\unexpected.bin').ToLowerInvariant()] = 'UNKNOWN-CONTENT'
Assert-Throws { & $entry -Stage 'Cleanup' @splat85 } 'Group B: an unknown file in the same container is NOT exempted' 'cleanup incomplete'
Assert-Equal ([string]$os85.State['Files'][(Join-Path $contract['ExperimentRoot'] 'broker\unexpected.bin').ToLowerInvariant()]) 'UNKNOWN-CONTENT' 'the unknown file bytes are preserved (never deleted by the retained-container logic)'
$ledger85 = Invoke-D4File -Boundary $os85.Boundary -Operation 'ReadText' -Arguments @($ledger85Path) | ConvertFrom-Json
$brokerDir85B = @($ledger85.entries | Where-Object { $_.path -like '*\broker' -and $_.kind -eq 'dir' })[0]
Assert-True ([string]$brokerDir85B.status -eq 'failed-clean') 'the container is NOT exempted when it holds unknown content'
# Group B2 (nested container): a nested ledger dir carrying ONLY its
# retained child is retained as well, and the parent chain follows - but an
# unknown file inside the nested container still blocks IT (never masked).
$os85b = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$sch85b = New-D4FakeSchtasks -Oses $os85b
$acl85b = New-D4HarnessAclRunner -Oses $os85b
$rules85b = New-D4FakeRuleStore -CreatedFacts $createdFacts
$pair85b = New-D4LifecycleNetPair -SchRunner $sch85b.Runner -ExperimentRoot $contract['ExperimentRoot']
$splat85b = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pair85b.Schtasks `
    -Net $pair85b.Net -Proc $pair85b.Proc `
    -Rules $rules85b.Runner -Os $os85b -Acl $acl85b.Runner -Installer $brokerInstaller -Store $storeFactory
$ledger85bPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
foreach ($s in @('S0', 'S1')) {
    & $entry -Stage $s @splat85b | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('R85 nested stage ' + $s + ' exits 0')
}
$os85b.State['Dirs'][(Join-Path $contract['ExperimentRoot'] 'broker\recovery').ToLowerInvariant()] = $true
$os85b.State['Files'][(Join-Path $contract['ExperimentRoot'] 'broker\recovery\state.bin').ToLowerInvariant()] = 'STATE-BYTES'
$ledger85b = ConvertTo-D4LedgerHashtable (Invoke-D4File -Boundary $os85b.Boundary -Operation 'ReadText' -Arguments @($ledger85bPath) | ConvertFrom-Json)
$ledger85b = Add-D4IntentLedgerEntry -Ledger $ledger85b -LedgerPath $ledger85bPath -Path (Join-Path $contract['ExperimentRoot'] 'broker\recovery') -Kind 'dir' -FileBoundary $os85b.Boundary
$ledger85b = Add-D4IntentLedgerEntry -Ledger $ledger85b -LedgerPath $ledger85bPath `
    -Path (Join-Path $contract['ExperimentRoot'] 'broker\recovery\state.bin') -Kind 'deployment-fact' `
    -Sha256 (Invoke-D4File -Boundary $os85b.Boundary -Operation 'Hash' -Arguments @((Join-Path $contract['ExperimentRoot'] 'broker\recovery\state.bin'))) `
    -FileBoundary $os85b.Boundary
$ledger85b = Save-D4DynamicRetention -LedgerPath $ledger85bPath -PackageHash $pkg -RunId $runId `
    -ExperimentRoot $contract['ExperimentRoot'] -RelativePaths @('broker\recovery\state.bin') `
    -Retention 'nested retained child; kept with its parent container' -FileBoundary $os85b.Boundary
$cleanup85C = @(& $entry -Stage 'Cleanup' @splat85b 2>&1)
Assert-Equal $LASTEXITCODE 0 'Group B2 (nested): a nested container carrying ONLY retained children is retained, not blocking'
$ledger85b = Invoke-D4File -Boundary $os85b.Boundary -Operation 'ReadText' -Arguments @($ledger85bPath) | ConvertFrom-Json
$recoveryDir85b = @($ledger85b.entries | Where-Object { $_.path -like '*\broker\recovery' -and $_.kind -eq 'dir' })[0]
Assert-True ([string]$recoveryDir85b.status -eq 'retained') 'the nested container is RETAINED with its child'
$brokerDir85b = @($ledger85b.entries | Where-Object { $_.path -like '*\broker' -and $_.kind -eq 'dir' })[0]
Assert-True ([string]$brokerDir85b.status -eq 'retained') 'the parent container chain propagates the retained terminal'
$os85b.State['Files'][(Join-Path $contract['ExperimentRoot'] 'broker\recovery\unexpected2.bin').ToLowerInvariant()] = 'UNKNOWN-NESTED'
Assert-Throws { & $entry -Stage 'Cleanup' @splat85b } 'Group B3: an unknown file inside the NESTED container is NOT exempted' 'cleanup incomplete'
Assert-Equal ([string]$os85b.State['Files'][(Join-Path $contract['ExperimentRoot'] 'broker\recovery\unexpected2.bin').ToLowerInvariant()]) 'UNKNOWN-NESTED' 'the nested unknown bytes are preserved'

# ---- P4 preview: zero host writes ----------------------------------------------
Write-Host '== P4 preview: zero host writes (default entry) =='
$previewOut = & $entry -Stage 'Inventory' 2>&1
Assert-True (($previewOut -join "`n") -match 'D4-P PREVIEW') 'the default entry prints the pure preview'

# §93/§95 password-managed share logon: account bootstrap + protected store +
# shared full-shape verification + T0 recovery (appended test section;
# ASCII-only for PS 5.1 no-BOM safety)

function New-D4FakeAccountRunner {
    # In-memory account/credential OS model for the §93/§95 flows. The LIVE
    # share-task model is the CALLER-SUPPLIED fake-schtasks Tasks map, so the
    # registered task is visible to the entry's later Confirm steps. The
    # password VALUE is never stored or printed - only class facts (received)
    # and a per-call pw-length token (flow proof) are recorded.
    param([hashtable]$TaskModel,
          [string]$SeedSid = $null,
          [bool]$CreateFails = $false,
          [bool]$RegisterFails = $false,
          [bool]$QueryAccountFails = $false,
          [bool]$QueryAccountFailsAfterCreate = $false,
          [object]$SeedIsAdmin = $false,
          [object]$SeedEnabled = $true)
    $st = @{
        Calls = New-Object System.Collections.Generic.List[string]
        AccountPresent = (-not [string]::IsNullOrEmpty($SeedSid))
        AccountSid = $SeedSid
        CreateFails = $CreateFails
        RegisterFails = $RegisterFails
        QueryAccountFails = $QueryAccountFails
        QueryAccountFailsAfterCreate = $QueryAccountFailsAfterCreate
        SeedIsAdmin = $SeedIsAdmin
        SeedEnabled = $SeedEnabled
        LastPasswordFacts = $null
        RegisteredCount = 0
        CreateCount = 0
        RegisterOmitsTask = $false
        RegisterS4UOverride = $false
        RegisterHighestOverride = $false
    }
    return @{
        State = $st
        QueryAccount = {
            param([string]$Name, [hashtable]$Ctx)
            $st['Calls'].Add('account query ' + $Name)
            if ($st['QueryAccountFails'] -or ($st['QueryAccountFailsAfterCreate'] -and $st['CreateCount'] -gt 0)) {
                return @{ present = $null; sid = $null; enabled = $null; is_admin = $null; error = 'simulated query failure' }
            }
            if (-not $st['AccountPresent']) {
                return @{ present = $false; sid = $null; enabled = $null; is_admin = $null; error = $null }
            }
            return @{ present = $true; sid = [string]$st['AccountSid']; enabled = $st['SeedEnabled']
                      is_admin = $st['SeedIsAdmin']; error = $null }
        }.GetNewClosure()
        CreateAccount = {
            param([string]$Name, [System.Security.SecureString]$Password, [hashtable]$Ctx)
            $st['Calls'].Add('account create ' + $Name)
            $st['CreateCount']++
            $st['LastPasswordFacts'] = Get-D4PasswordClassFacts -Password $Password
            if ($st['CreateFails']) { return @{ ok = $false; sid = $null; error = 'simulated create failure' } }
            if ($st['AccountPresent']) { return @{ ok = $false; sid = $null; error = 'already exists' } }
            $st['AccountPresent'] = $true
            $st['AccountSid'] = 'S-1-5-21-777-77-' + [string](1000 + $st['CreateCount'] * 100)
            return @{ ok = $true; sid = $st['AccountSid']; error = $null }
        }.GetNewClosure()
        DisableAccount = {
            param([string]$Name, [hashtable]$Ctx)
            $st['Calls'].Add('account disable ' + $Name)
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
        RegisterShareTask = {
            param([string]$TaskName, [string]$TaskPath, [string]$DefinitionXml,
                  [string]$UserName, [string]$PlainPassword, [hashtable]$Ctx)
            $st['Calls'].Add('task register ' + $TaskName + ' user=' + $UserName +
                             ' pwlen=' + [string]$PlainPassword.Length +
                             ' xmlbytes=' + [string]$DefinitionXml.Length)
            # Folder+name semantics: only the ROOT folder ('\') is a legal
            # TaskPath for this contract - anything else would land the task
            # in a subfolder the query/cleanup never addresses.
            if ([string]$TaskPath -ne '\') { return @{ ok = $false; error = 'simulated: TaskPath must be the root folder \, got ' + [string]$TaskPath } }
            if ($st['RegisterFails']) { return @{ ok = $false; error = 'simulated registration failure' } }
            if ($TaskModel.ContainsKey($TaskName.ToLowerInvariant())) { return @{ ok = $false; error = 'task already exists' } }
            if ($st['RegisterOmitsTask']) { return @{ ok = $true; error = $null } }
            $storedXml = $DefinitionXml
            if ($st['RegisterS4UOverride']) {
                $storedXml = $DefinitionXml -replace '<LogonType>Password</LogonType>', '<LogonType>S4U</LogonType>'
            }
            if ($st['RegisterHighestOverride']) {
                $storedXml = $storedXml -replace '<RunLevel>LeastPrivilege</RunLevel>', '<RunLevel>HighestAvailable</RunLevel>'
            }
            $TaskModel[$TaskName.ToLowerInvariant()] = $storedXml
            $st['RegisteredCount']++
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
        QueryTask = {
            param([string]$TaskName, [hashtable]$Ctx)
            $st['Calls'].Add('task query ' + $TaskName)
            if ($TaskModel.ContainsKey($TaskName.ToLowerInvariant())) {
                return @{ present = $true; xml = $TaskModel[$TaskName.ToLowerInvariant()]; error = $null }
            }
            return @{ present = $false; xml = $null; error = $null }
        }.GetNewClosure()
    }
}

function New-D4FakeStoreGuard {
    # Fake protected-store guard for the §95.3 refusal paths: records the
    # call, can simulate a structured failure or a THROW (both must refuse
    # the whole bootstrap with zero account/register actions).
    param([bool]$Fails = $false, [bool]$Throws = $false)
    $st = @{
        Calls = New-Object System.Collections.Generic.List[string]
        Fails = $Fails
        Throws = $Throws
    }
    return @{
        State = $st
        Guard = {
            param([string]$Dir, [hashtable]$Ctx)
            $st['Calls'].Add('store guard ' + $Dir)
            if ($st['Throws']) { throw 'simulated guard failure' }
            if ($st['Fails']) { return @{ ok = $false; error = 'simulated unprotected store' } }
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
    }
}

$pwPkg = 'pwfixture0000000000000000000000000000000000000000000000000000000001'
# the fixed plan-identity fixture for the pure bootstrap tests (equality-bound;
# the ENTRY flows compute the real one from the plan/config)
$pwPlanIdentity = 'planfixture0000000000000000000000000000000000000000000000000000000001'

$noReparseProbe = { param([string]$Path) return $null }

function New-D4FakeAclRule {
    # a faithful GetAccessRules element shape: IdentityReference (with
    # .Value), AccessControlType (a string carries .ToString()) and the
    # REAL FileSystemRights enum value
    # the Rights value is carried RAW (a real enum value OR an ACCESS_MASK
    # bit like GenericAll 0x10000000 which is NOT an enum member) - the
    # real probe consumes the numeric bits, so the fake rule just carries
    # them
    param([string]$IdentitySid, [string]$Type, [object]$Rights)
    return [pscustomobject]@{
        IdentityReference = [pscustomobject]@{ Value = [string]$IdentitySid }
        AccessControlType = [string]$Type
        FileSystemRights = $Rights
    }
}
function New-D4FakeSecurityDescriptor {
    # §101.2: an interface-conforming STAND-IN for the exception/negative
    # branches only - .GetOwner([type]) and
    # .GetAccessRules($true,$true,SecurityIdentifier) match the real
    # ObjectSecurity interface. The POSITIVE cases use REAL in-memory
    # .NET descriptors (New-D4RealInMemoryFileSecurity) - a fabricated
    # shape must never again mask a product interface bug
    param([string]$OwnerSid, [object[]]$Rules)
    $sid = [string]$OwnerSid
    $ruleObjects = @()
    foreach ($r in @($Rules)) {
        # inputs may be hashtables OR rule objects (PSCustomObject has NO
        # string indexer - property access only)
        if ($r -is [hashtable]) {
            $ruleObjects += (New-D4FakeAclRule -IdentitySid ([string]$r['identitySid']) -Type ([string]$r['type']) -Rights $r['rights'])
        } else {
            $ruleObjects += (New-D4FakeAclRule -IdentitySid ([string]$r.IdentityReference.Value) -Type ([string]$r.AccessControlType) -Rights $r.FileSystemRights)
        }
    }
    $sd = [pscustomobject]@{ dummy = 1 }
    # GetOwner lives ON THE DESCRIPTOR (ObjectSecurity.GetOwner) - the
    # stand-in mirrors that placement, not Owner.Translate
    $sd | Add-Member -MemberType ScriptMethod -Name GetOwner -Value {
        param([type]$targetType)
        return [pscustomobject]@{ Value = [string]$sid }
    }.GetNewClosure()
    $sd | Add-Member -MemberType ScriptMethod -Name GetAccessRules -Value {
        param([bool]$includeExplicit, [bool]$includeInherited, [type]$targetType)
        return [object[]]$ruleObjects
    }.GetNewClosure()
    return $sd
}
function New-D4FakeGetAclEdge {
    # fake Get-Acl EDGE: scripts the SecurityDescriptor per path (or an
    # always-trusted mode), or throws (the failure mode) - ONLY this edge
    # is fake; the real probe's translation/decision code runs unchanged
    param([hashtable]$DescriptorByPath, [switch]$AlwaysTrusted, [switch]$Fails)
    $st = @{
        Calls = New-Object System.Collections.Generic.List[string]
        DescriptorByPath = $DescriptorByPath
        AlwaysTrusted = [bool]$AlwaysTrusted
        Fails = [bool]$Fails
    }
    return @{
        State = $st
        Edge = {
            param([string]$Path)
            $st['Calls'].Add('getacl ' + $Path)
            if ($st['Fails']) { throw 'simulated ACL query failure' }
            $key = $Path.ToLowerInvariant()
            if ($st['AlwaysTrusted']) {
                # §101.3: the trusted mode returns a REAL in-memory .NET
                # descriptor (SYSTEM owner + SYSTEM/Admin FullControl) -
                # GetOwner/GetAccessRules are real .NET methods here
                return (New-D4RealInMemoryFileSecurity -OwnerSid 'S-1-5-18' -Rules @(
                    @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
                    @{ identitySid = 'S-1-5-32-544'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl }))
            }
            if (-not $st['DescriptorByPath'].ContainsKey($key)) { return $null }
            return $st['DescriptorByPath'][$key]
        }.GetNewClosure()
    }
}
function New-D4RealInMemoryFileSecurity {
    # §101.3: a REAL .NET FileSecurity object built ENTIRELY in memory
    # (SetOwner + AddAccessRule with SecurityIdentifier identities - no
    # real-path Get-Acl, no Set-Acl, no disk) - GetOwner/GetAccessRules
    # are REAL .NET methods on it, so the positive cases can never be
    # validated against a fabricated shape again
    param([string]$OwnerSid, [object[]]$Rules)
    $sd = New-Object System.Security.AccessControl.FileSecurity
    $sd.SetOwner(([System.Security.Principal.SecurityIdentifier][string]$OwnerSid))
    foreach ($r in @($Rules)) {
        # inputs may be hashtables OR rule objects (PSCustomObject has NO
        # string indexer - property access only)
        if ($r -is [hashtable]) {
            $sidRef = [System.Security.Principal.SecurityIdentifier][string]$r['identitySid']
            $accType = [System.Security.AccessControl.AccessControlType][string]$r['type']
            $rightsVal = [System.Security.AccessControl.FileSystemRights]$r['rights']
        } else {
            $sidRef = [System.Security.Principal.SecurityIdentifier]$r.IdentityReference.Value
            $accType = [System.Security.AccessControl.AccessControlType]$r.AccessControlType
            $rightsVal = $r.FileSystemRights
        }
        $sd.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sidRef, $rightsVal, $accType)))
    }
    return $sd
}
# the DEFAULT trusted probe: the REAL probe over the always-trusted fake edge
$script:r97TrustedAclProbe = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -AlwaysTrusted).Edge



$reparseProbe = {
    param([string]$Path)
    if ([string]$Path -eq 'C:\ProgramData\UMShareExp-bootstrap') {
        $item = New-Object PSObject
        return @{ Attributes = [IO.FileAttributes]::ReparsePoint }
    }
    return $null
}

# ---- §95.2-A: REAL runner call shape (metadata, never executed) --------------
Write-Host '== §95-A: real runner interface =='
Import-Module ScheduledTasks -ErrorAction SilentlyContinue
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction SilentlyContinue
$regCmdMeta = Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue
Assert-True ($null -ne $regCmdMeta) 'Register-ScheduledTask cmdlet metadata is available'
$xmlSet = @($regCmdMeta.ParameterSets | Where-Object { $_.Name -eq 'Xml' })[0]
$xmlSetParams = @($xmlSet.Parameters.Name)
Assert-True (($xmlSetParams -contains 'Xml') -and ($xmlSetParams -contains 'User') -and ($xmlSetParams -contains 'Password')) 'the REAL Xml parameter set carries Xml+User+Password (Register-ScheduledTask)'
$setLuMeta = Get-Command Set-LocalUser -ErrorAction SilentlyContinue
Assert-True ($null -eq $setLuMeta -or -not $setLuMeta.Parameters.ContainsKey('Enabled')) 'Set-LocalUser has NO -Enabled parameter (the old adapter call was invalid)'
Assert-True ($null -ne (Get-Command Disable-LocalUser -ErrorAction SilentlyContinue)) 'Disable-LocalUser exists (the real disable interface)'
$acctRunnerReal = New-D4AccountRunner
$regSrcAll = [string]$acctRunnerReal['RegisterShareTask']
$regSrc = (@($regSrcAll -split "`n") | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
Assert-True (($regSrc -match 'Register-ScheduledTask') -and ($regSrc -match '-Xml')) 'the real register runner consumes the definition XML directly (Register-ScheduledTask -Xml)'
Assert-True (-not ($regSrc -match 'New-ScheduledTask')) 'the real register runner NEVER calls New-ScheduledTask (no -Xml parameter exists there)'
Assert-True (($regSrc -match '-User') -and ($regSrc -match '-Password')) 'the credential travels only as cmdlet parameters (no argv/env/file)'
Assert-True (-not ($regSrc -match '-Force')) 'no overwrite: -Force is intentionally absent from the real register runner'
$disSrcAll = [string]$acctRunnerReal['DisableAccount']
$disSrc = (@($disSrcAll -split "`n") | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
Assert-True (($disSrc -match 'Disable-LocalUser') -and (-not ($disSrc -match 'Set-LocalUser'))) 'the real disable runner uses Disable-LocalUser (never Set-LocalUser -Enabled)'

# ---- §95.5-D: shared FULL task-shape verifier ---------------------------------
Write-Host '== §95-D: shared full task-shape verifier =='
$shapeSid = 'S-1-5-21-777-77-9999'
$shapeCmd = 'C:\ProgramData\UMShareExp-broker\run-live.cmd'
$shapeTask = @{ Name = 'umshare-share-exp'; IdentitySid = $shapeSid; LogonType = 'Password';
                Command = $shapeCmd; TaskPath = '\'; Trigger = 'ONDEMAND' }
$shapeOwned = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } `
    -CommandPath $shapeCmd -LogonType 'Password'
$shapeOwnedVerdict = Test-D4TaskDefinitionShape -DefinitionXml $shapeOwned -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password'
Assert-Equal $shapeOwnedVerdict.State 'owned' 'the full-shape verifier owns a correct Password definition'
$shapeWrongCmd = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } `
    -CommandPath 'C:\evil\other.cmd' -LogonType 'Password'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeWrongCmd -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'same SID+Password with a WRONG command is NOT owned'
$shapeTrigger = $shapeOwned -replace '<Triggers />', '<Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeTrigger -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'an automatic trigger refuses the full shape'
$shapeHighRun = $shapeOwned -replace '<RunLevel>LeastPrivilege</RunLevel>', '<RunLevel>HighestAvailable</RunLevel>'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeHighRun -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'a Password definition demanding HighestAvailable is a different security context'
$shapeS4U = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath $shapeCmd
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeS4U -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'an S4U definition is NEVER claimed as a Password task'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeS4U -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $shapeSid; LogonType = 'S4U'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'S4U').State 'owned' 'the S4U variant owns with its own logon context'

# ---- §210.3 W1: Principal name -> SID REAL identity resolution -------------
# §210 W2: Task Scheduler OMITS <RunLevel> in the schtasks /XML re-export on
# this host; an ABSENT RunLevel takes the logon-implied default - same
# security context, and explicit wrong values still refuse.
$shapeNoRunLevel = $shapeOwned -replace '<RunLevel>LeastPrivilege</RunLevel>', ''
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeNoRunLevel -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'owned' 'an ABSENT RunLevel with Password owns (OS-imposed LeastPrivilege default, host strips the field on re-export)'
$shapeHighRun2 = $shapeNoRunLevel -replace '<LogonType>Password</LogonType>', '<LogonType>Password</LogonType><RunLevel>HighestAvailable</RunLevel>'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeHighRun2 -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'an explicit WRONG RunLevel still refuses (HighestAvailable with Password)'
$shapeS4UNoRL = $shapeS4U -replace '<RunLevel>HighestAvailable</RunLevel>', ''
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeS4UNoRL -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $shapeSid; LogonType = 'S4U'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'S4U').State 'owned' 'an ABSENT RunLevel with S4U owns (OS-imposed HighestAvailable default)'

# §210 W2: Task Scheduler ALSO omits AllowStartOnDemand on re-export; absent =
# OS default true; explicit false still refuses.
$shapeNoAOD = $shapeOwned -replace '<AllowStartOnDemand>true</AllowStartOnDemand>', ''
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeNoAOD -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'owned' 'an ABSENT AllowStartOnDemand owns (OS default true, host strips the element on re-export)'
$shapeFalseAOD = $shapeOwned -replace '<AllowStartOnDemand>true</AllowStartOnDemand>', '<AllowStartOnDemand>false</AllowStartOnDemand>'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeFalseAOD -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'an explicit AllowStartOnDemand=false still refuses'

# §210 W2: Task Scheduler omits <LogonType> for a WELL-KNOWN SERVICE principal
# with the S4U logon on this host - accepted only for SYSTEM+S4U; a Password
# task with an absent LogonType always refuses.
$shapeSysS4U = $shapeOwned -replace 'S-1-5-21-777-77-9999', 'S-1-5-18' -replace '<LogonType>Password</LogonType>', '' -replace '<RunLevel>LeastPrivilege</RunLevel>', ''
$sysTaskSpec = @{ Name = 'umshare-broker-exp'; IdentitySid = 'S-1-5-18'; LogonType = 'S4U'; Command = $shapeCmd; Trigger = 'ONDEMAND' }
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeSysS4U -Task $sysTaskSpec -Trigger 'ONDEMAND' -LogonType 'S4U').State 'owned' 'an ABSENT LogonType with SYSTEM principal + S4U owns (OS-implied unique S4U context)'
$shapeNoLogonPW = $shapeOwned -replace '<LogonType>Password</LogonType>', ''
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeNoLogonPW -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'an ABSENT LogonType with Password still refuses (logon context never implied for interactive tasks)'


Write-Host '== §210-W1: principal name->SID real identity resolution =='
# GroupId-only (group) principal is NEVER treated as a User principal
$shapeGroupId = $shapeOwned -replace '<UserId>', '<GroupId>' -replace '</UserId>', '</GroupId>'
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeGroupId -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'a GroupId-only group principal is refused (never treated as a User)'
# the injectable identity edge is consumed (offline double)
$fakeResolverOk = { param([string]$t) return @{ ok = $true; sid = $shapeSid; form = 'fake' } }.GetNewClosure()
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeOwned -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password' -IdentityResolver $fakeResolverOk).State 'owned' 'the identity-resolution edge is consumed (offline double -> owned)'
$fakeResolverBad = { param([string]$t) return @{ ok = $false; sid = ''; reason = 'fake resolution failure' } }.GetNewClosure()
Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeOwned -Task $shapeTask -Trigger 'ONDEMAND' -LogonType 'Password' -IdentityResolver $fakeResolverBad).State 'unconfirmed' 'a resolution failure through the edge is unconfirmed (never owned)'
# REAL Windows read-only resolution: the deployed local account NAME (the
# run06 failure shape) resolves to the fixed SID through the real default
# edge; foreign/unresolvable principals refuse.
$realUser = Get-LocalUser -Name 'um_share_d4' -ErrorAction SilentlyContinue
if ($null -ne $realUser) {
    $realSid = [string]$realUser.SID.Value
    $shapeNameForm = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = 'um_share_d4' } -CommandPath $shapeCmd -LogonType 'Password'
    Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeNameForm -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $realSid; LogonType = 'Password'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'Password').State 'owned' 'an exported local account NAME resolves (REAL Windows edge) to the fixed SID and owns'
    $shapeNameForm2 = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = ($env:COMPUTERNAME + '\um_share_d4') } -CommandPath $shapeCmd -LogonType 'Password'
    Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeNameForm2 -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $realSid; LogonType = 'Password'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'Password').State 'owned' 'a machine-qualified exported name resolves to the fixed SID and owns'
    $shapeForeignName = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = 'DefinitelyNoSuchUserXYZ' } -CommandPath $shapeCmd -LogonType 'Password'
    Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeForeignName -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $realSid; LogonType = 'Password'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'Password').State 'unconfirmed' 'an unresolvable exported principal is unconfirmed (never owned)'
    $shapeGroupSid = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = 'S-1-5-32-544' } -CommandPath $shapeCmd -LogonType 'Password'
    Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeGroupSid -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $realSid; LogonType = 'Password'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'a group SID principal is mismatched (never owned)'
    $shapeWrongSid = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = 'S-1-5-21-555-55-5555' } -CommandPath $shapeCmd -LogonType 'Password'
    Assert-Equal (Test-D4TaskDefinitionShape -DefinitionXml $shapeWrongSid -Task (@{ Name = 'umshare-share-exp'; IdentitySid = $realSid; LogonType = 'Password'; Command = $shapeCmd }) -Trigger 'ONDEMAND' -LogonType 'Password').State 'mismatch' 'a different SID literal is mismatched'
} else {
    Write-Host 'SKIP §210-W1 real-account-name cases (um_share_d4 absent on this host)'
}

# ---- §95.3-B: protected store + record binding --------------------------------
Write-Host '== §95-B: protected store + record bindings =='
$planRuleIds = @('11111111-1111-4111-8111-111111111111',
                 '22222222-2222-4222-8222-222222222222',
                 '33333333-3333-4333-8333-333333333333')
$rtFilePw = @(@{ file = 'python-core.bin'; sha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' })
$planOs = New-D4InMemoryOs -Root 'C:\ProgramData\UMShareExp-bootstrap'
$planPath = 'C:\ProgramData\UMShareExp-bootstrap\plan.json'
Invoke-D4File -Boundary $planOs.Boundary -Operation 'WriteText' -Arguments @($planPath,
    (@{ run_id = '20260910T120000-aa11bb22'; account_name = 'um_share_d4';
        python_home = 'C:\PythonExp'; runtime_source_dir = 'C:\ProgramData\UMShareRun';
        runtime_files = $rtFilePw; rule_ids = $planRuleIds } | ConvertTo-Json -Depth 5))
$planOk = Read-D4BootstrapPlan -Path $planPath -FileBoundary $planOs.Boundary -AllowedAccount 'um_share_d4'
Assert-Equal $planOk['account_name'] 'um_share_d4' 'bootstrap plan carries the fixed account name'
Assert-Equal (@($planOk['rule_ids']) -join ',') ($planRuleIds -join ',') 'bootstrap plan carries the FIXED rule ids (recovery re-emits the same config)'
Assert-Throws { Read-D4BootstrapPlan -Path $planPath -FileBoundary $planOs.Boundary -AllowedAccount 'some_other_exp_account' } 'an account outside the experiment whitelist refuses' 'whitelist'
$evilPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\evil.json'
Invoke-D4File -Boundary $planOs.Boundary -Operation 'WriteText' -Arguments @($evilPlanPath,
    (@{ run_id = '20260910T120000-aa11bb22'; account_name = 'um_share_d4'; python_home = 'p';
        runtime_source_dir = 'r'; runtime_files = $rtFilePw } | ConvertTo-Json -Depth 4))
Assert-Throws { Read-D4BootstrapPlan -Path $evilPlanPath -FileBoundary $planOs.Boundary -AllowedAccount 'um_share_d4' } 'a plan without the fixed rule ids refuses' 'rule_ids'
$missingPlan = 'C:\ProgramData\UMShareExp-bootstrap\missing.json'
Assert-Throws { Read-D4BootstrapPlan -Path $missingPlan -FileBoundary $planOs.Boundary -AllowedAccount 'um_share_d4' } 'missing plan refuses' 'not found'

# ---- §95.3-B: store guard + pre-existing record refusal (BEFORE any action) ---
Write-Host '== §95-B: store guard refusals =='
$acctRunId = '20260910T120000-aa11bb22'
$bootRoot = 'C:\ProgramData\UMShareExp-bootstrap'
$bootStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$defBuilder = {
    param([string]$Sid)
    New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $Sid } `
        -CommandPath 'C:\ProgramData\UMShareExp-broker\run-live.cmd' -LogonType 'Password'
}.GetNewClosure()
# guard failure -> REFUSED before ANY account/register action
$bootOsG = New-D4InMemoryOs -Root $bootRoot
$bootSchG = New-D4FakeSchtasks -Oses $bootOsG
$acctG = New-D4FakeAccountRunner -TaskModel $bootSchG.State['Tasks']
$guardFail = New-D4FakeStoreGuard -Fails $true
$rG = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctG -FileBoundary $bootOsG.Boundary `
    -StoreGuard $guardFail.Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rG.Ok -and $rG.Refused -eq 'store-not-protected') 'an unprotected store refuses the whole bootstrap'
Assert-Equal $acctG.State['CreateCount'] 0 'guard failure costs ZERO account-create calls'
Assert-Equal $acctG.State['RegisteredCount'] 0 'guard failure costs ZERO register calls'
# guard throw -> same refusal
$guardThrow = New-D4FakeStoreGuard -Throws $true
$rGT = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctG -FileBoundary $bootOsG.Boundary `
    -StoreGuard $guardThrow.Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rGT.Ok) 'a throwing guard refuses the bootstrap (structured refusal, no crash path)'
# reparse ancestor -> refused before any action
$bootOsR = New-D4InMemoryOs -Root $bootRoot
$bootSchR = New-D4FakeSchtasks -Oses $bootOsR
$acctR = New-D4FakeAccountRunner -TaskModel $bootSchR.State['Tasks']
$rR = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctR -FileBoundary $bootOsR.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $reparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rR.Ok) 'a reparse-point bootstrap root refuses before any action'
Assert-Equal $acctR.State['CreateCount'] 0 'reparse refusal costs ZERO account-create calls'
# pre-seeded FORGED record -> refused, zero actions, never patched up
$bootOsF = New-D4InMemoryOs -Root $bootRoot
$bootSchF = New-D4FakeSchtasks -Oses $bootOsF
$acctF = New-D4FakeAccountRunner -TaskModel $bootSchF.State['Tasks']
$forged = @{ schema = 'd4-account-bootstrap'; run_id = $acctRunId; account_name = 'um_share_d4';
             task_name = $contract['ShareTaskName']; package_hash = $pwPkg; plan_identity = $pwPlanIdentity; status = 'task-registered';
             created_sid = 'S-1-5-21-777-77-0000'; intent_at = ''; created_at = '';
             task_registered_at = ''; unconfirmed_reason = ''; task_register_result = '' }
Invoke-D4File -Boundary $bootOsF.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($forged | ConvertTo-Json -Depth 4))
$rF = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctF -FileBoundary $bootOsF.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rF.Ok) 'a pre-seeded forged record (foreign SID, task-registered) is refused'
Assert-Equal $acctF.State['CreateCount'] 0 'forged record costs ZERO account-create calls'
Assert-Equal $acctF.State['RegisteredCount'] 0 'forged record costs ZERO register calls'
Assert-True ([string](Invoke-D4File -Boundary $bootOsF.Boundary -Operation 'ReadText' -Arguments @($bootStatePath)).Contains('S-1-5-21-777-77-0000')) 'the forged record bytes are UNCHANGED (no ACL patch-up, no rewrite)'
# record binding: wrong task name / wrong package / unknown field -> refuse
$bootOsB = New-D4InMemoryOs -Root $bootRoot
$bootSchB = New-D4FakeSchtasks -Oses $bootOsB
$basis = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$wrongTask = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName 'some-other-task' -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
Invoke-D4File -Boundary $bootOsB.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($wrongTask | ConvertTo-Json -Depth 4))
Assert-Throws { Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOsB.Boundary } 'a record bound to a DIFFERENT task refuses' 'belongs to task'
$wrongPkg = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash 'some-other-package-00000000000000000000000000000000' -PlanIdentity $pwPlanIdentity
Invoke-D4File -Boundary $bootOsB.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($wrongPkg | ConvertTo-Json -Depth 4))
Assert-Throws { Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOsB.Boundary } 'a record bound to a DIFFERENT package refuses (no silent reuse across builds)' 'belongs to package'
$extraField = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$extraField['sneaky_field'] = 'x'
Invoke-D4File -Boundary $bootOsB.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($extraField | ConvertTo-Json -Depth 4))
Assert-Throws { Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOsB.Boundary } 'an unknown field in the record refuses (fixed structure only)' 'field set invalid'
# a LEGITIMATE protected record round-trips within the same run
$bootOsL = New-D4InMemoryOs -Root $bootRoot
$bootSchL = New-D4FakeSchtasks -Oses $bootOsL
$acctL = New-D4FakeAccountRunner -TaskModel $bootSchL.State['Tasks']
$guardOk = New-D4FakeStoreGuard
$rL = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctL -FileBoundary $bootOsL.Boundary `
    -StoreGuard $guardOk.Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True ($rL.Ok -and $rL.Status -eq 'task-registered') 'a legitimately protected record flow completes (create+register once)'
Assert-True ((@($guardOk.State['Calls']) | Where-Object { $_ -match 'store guard' }).Count -ge 1) 'the protected store guard ran BEFORE any account action'
$recL = Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOsL.Boundary
Assert-Equal $recL.status 'task-registered' 'the legitimate record reads back within the same run'
Assert-True ([string]$recL.package_hash -eq $pwPkg) 'the record binds the package identity'

# ---- §95.5-D: account privilege facts are CONSUMED -----------------------------
Write-Host '== §95-D: account privilege consumption =='
foreach ($case in @(
    @{ tag = 'admin'; isAdmin = $true },
    @{ tag = 'admin-unknown'; isAdmin = $null },
    @{ tag = 'disabled'; isAdmin = $false; enabled = $false }
)) {
    $bootOsP = New-D4InMemoryOs -Root $bootRoot
    $bootSchP = New-D4FakeSchtasks -Oses $bootOsP
    $enabledP = $true
    if ($case.ContainsKey('enabled')) { $enabledP = $case['enabled'] }
    $acctP = New-D4FakeAccountRunner -TaskModel $bootSchP.State['Tasks'] -SeedSid 'S-1-5-21-777-77-7777' `
        -SeedIsAdmin $case['isAdmin'] -SeedEnabled $enabledP
    $recP = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
    $recP.status = 'task-registered'
    $recP.created_sid = 'S-1-5-21-777-77-7777'
    Invoke-D4File -Boundary $bootOsP.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($recP | ConvertTo-Json -Depth 4))
    $rP = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
        -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
        -StatePath $bootStatePath -AccountRunner $acctP -FileBoundary $bootOsP.Boundary `
        -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
    Assert-True (-not $rP.Ok) ('an account with ' + $case['tag'] + ' privilege facts is NOT reused')
    Assert-Equal $acctP.State['RegisteredCount'] 0 ('the ' + $case['tag'] + ' account receives ZERO register calls')
}

# ---- §95.4-C: registration ok but the EXPORT is missing -> unconfirmed --------
Write-Host '== §95-C: post-register export verification =='
$bootOsX = New-D4InMemoryOs -Root $bootRoot
$bootSchX = New-D4FakeSchtasks -Oses $bootOsX
$acctX = New-D4FakeAccountRunner -TaskModel $bootSchX.State['Tasks']
$acctX.State['RegisterOmitsTask'] = $true
$rX = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acctX -FileBoundary $bootOsX.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rX.Ok) 'a register call that reports ok but leaves NO exported task is unconfirmed (ok never substitutes for the export check)'
$recX = Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOsX.Boundary
Assert-Equal $recX.status 'unconfirmed' 'the record stays unconfirmed with the retained SID'
Assert-True ([string]$recX.created_sid) 'the SID is retained for the authorized disable/keep decision'

# ---- §93 PURE: Invoke-D4AccountBootstrap core flows (create-once etc.) --------
Write-Host '== §93 PURE: account bootstrap flows =='
$bootOs = New-D4InMemoryOs -Root $bootRoot
$bootSch = New-D4FakeSchtasks -Oses $bootOs
$acct1 = New-D4FakeAccountRunner -TaskModel $bootSch.State['Tasks']
$guard1 = New-D4FakeStoreGuard
$r1 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct1 -FileBoundary $bootOs.Boundary `
    -StoreGuard $guard1.Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True $r1.Ok 'absent account is created and registered once'
Assert-Equal $acct1.State['CreateCount'] 1 'exactly ONE account-create call'
Assert-Equal $acct1.State['RegisteredCount'] 1 'exactly ONE task-register call'
Assert-True ([string]$r1.CreatedSid) 'bootstrap returns the created SID'
$rec1 = Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOs.Boundary
Assert-Equal $rec1.status 'task-registered' 'record terminal state is task-registered'
Assert-True ([string]$rec1.created_sid -eq [string]$r1.CreatedSid) 'record SID equals the returned SID'
Assert-True ($bootSch.State['Tasks'].ContainsKey($contract['ShareTaskName'].ToLowerInvariant())) 'the registered task landed in the fake scheduler'
Assert-True ($bootSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()].Contains('<LogonType>Password</LogonType>')) 'registered definition is Password logon'
Assert-True ($bootSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()].Contains([string]$r1.CreatedSid)) 'registered definition carries the created SID'
Assert-True (($rec1 | ConvertTo-Json -Depth 8) -notmatch '"password"|"secret"|"credential"') 'bootstrap record carries NO secret fields'
$callLines = @($acct1.State['Calls'])
Assert-True ((@($callLines | Where-Object { $_ -match 'pw=' }).Count) -eq 0) 'call log NEVER logs a password value'
Assert-True ((@($callLines | Where-Object { $_ -match 'pwlen=' }).Count) -eq 1) 'call log proves a credential flowed (length only)'
Assert-True ($null -ne $acct1.State['LastPasswordFacts']) 'the credential entered the account boundary (class facts recorded)'
# idempotent re-entry: registered + matching -> NO re-create, NO re-register
$acct1b = New-D4FakeAccountRunner -TaskModel $bootSch.State['Tasks'] -SeedSid ([string]$r1.CreatedSid)
$r1b = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct1b -FileBoundary $bootOs.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True ($r1b.Ok) 'a registered+owned run is idempotent (reuse, no credential needed)'
Assert-Equal $acct1b.State['CreateCount'] 0 'idempotent re-entry performs ZERO re-creation'
Assert-Equal $acct1b.State['RegisteredCount'] 0 'idempotent re-entry performs ZERO re-registration'
# foreign same-name account -> ZERO create/register, explicit refusal
$bootOs2 = New-D4InMemoryOs -Root $bootRoot
$bootSch2 = New-D4FakeSchtasks -Oses $bootOs2
$acct2 = New-D4FakeAccountRunner -TaskModel $bootSch2.State['Tasks'] -SeedSid 'S-1-5-21-777-77-8888'
$r2 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct2 -FileBoundary $bootOs2.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $r2.Ok) 'a foreign same-name account is refused'
Assert-True ($r2.Refused -eq 'foreign-or-unprovable') 'foreign refusal reason is explicit'
Assert-Equal $acct2.State['CreateCount'] 0 'foreign account receives ZERO create calls'
Assert-Equal $acct2.State['RegisteredCount'] 0 'foreign account receives ZERO register calls'
Assert-True ($bootSch2.State['Tasks'].Count -eq 0) 'foreign run registers NO task'
# creation failure / query unconfirmed -> explicit unconfirmed, zero side effects
$bootOs4 = New-D4InMemoryOs -Root $bootRoot
$bootSch4 = New-D4FakeSchtasks -Oses $bootOs4
$acct4 = New-D4FakeAccountRunner -TaskModel $bootSch4.State['Tasks'] -CreateFails $true
$r4 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct4 -FileBoundary $bootOs4.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $r4.Ok -and $r4.Refused -eq 'unconfirmed') 'a failed creation is an explicit unconfirmed'
Assert-Equal $acct4.State['RegisteredCount'] 0 'a failed creation registers NOTHING'
$bootOs5 = New-D4InMemoryOs -Root $bootRoot
$bootSch5 = New-D4FakeSchtasks -Oses $bootOs5
$acct5 = New-D4FakeAccountRunner -TaskModel $bootSch5.State['Tasks'] -QueryAccountFails $true
$r5 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct5 -FileBoundary $bootOs5.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $r5.Ok -and $r5.Refused -eq 'unconfirmed') 'an unconfirmed account query refuses (never absence)'
Assert-Equal $acct5.State['CreateCount'] 0 'an unconfirmed query creates NOTHING'
# registration failure -> unconfirmed, SID KEPT, NO new password on re-entry
$bootOs3 = New-D4InMemoryOs -Root $bootRoot
$bootSch3 = New-D4FakeSchtasks -Oses $bootOs3
$acct3 = New-D4FakeAccountRunner -TaskModel $bootSch3.State['Tasks'] -RegisterFails $true
$r3 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct3 -FileBoundary $bootOs3.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $r3.Ok -and $r3.Refused -eq 'unconfirmed' -and $r3.CreatedSid) 'registration failure stops with unconfirmed + retained SID'
$rec3 = Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $bootOs3.Boundary
Assert-Equal $rec3.status 'unconfirmed' 'record is unconfirmed after a registration failure'
Assert-True ([string]$rec3.created_sid) 'created SID is retained in the record'
$acct3b = New-D4FakeAccountRunner -TaskModel $bootSch3.State['Tasks'] -SeedSid ([string]$rec3.created_sid)
$r3b = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $acct3b -FileBoundary $bootOs3.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $r3b.Ok) 're-entry after a failed registration does NOT continue without the credential'
Assert-Equal $acct3b.State['CreateCount'] 0 'NO password regeneration / re-create on re-entry'
# missing runner -> fail closed with zero OS calls
Assert-Throws { Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $null -FileBoundary $bootOs3.Boundary `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity } 'missing account runner fails closed' 'account runner'

# ---- §93: identity/logon gate + cleanup account decision ----------------------
Write-Host '== §93: Confirm-D4TaskIdentity LogonType gate + cleanup decision =='
$idOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$idSch = New-D4FakeSchtasks -Oses $idOs
$idTask = @{ Name = $contract['ShareTaskName']; IdentitySid = $shapeSid; LogonType = 'Password'; Command = $shapeCmd }
$idDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath $idTask['Command'] -LogonType 'Password'
$idSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $idDef
$idOwned = Confirm-D4TaskIdentity -Task $idTask -Trigger 'ONDEMAND' -SchtasksRunner $idSch.Runner -LogonType 'Password'
Assert-Equal $idOwned.State 'owned' 'a matching Password task with the expected SID is owned'
$idTaskS4UExpect = @{ Name = $contract['ShareTaskName']; IdentitySid = $shapeSid; LogonType = 'S4U'; Command = $shapeCmd }
$idMismatch = Confirm-D4TaskIdentity -Task $idTaskS4UExpect -Trigger 'ONDEMAND' -SchtasksRunner $idSch.Runner -LogonType 'S4U'
Assert-True ($idMismatch.State -eq 'mismatch' -and $idMismatch.Reason -match 'logonType') 'a SID match with the WRONG logon type is NOT owned (refuses claim/cleanup)'
$cleanupRec = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$cleanupRec.status = 'task-registered'
$cleanupRec.created_sid = $shapeSid
$cdForeign = Test-D4CleanupAccountDecision -BootstrapState $cleanupRec -LiveSid 'S-1-5-21-777-77-0000'
Assert-Equal $cdForeign.Decision 'refuse' 'cleanup decision REFUSES without the same-SID proof'
$cdKeep = Test-D4CleanupAccountDecision -BootstrapState $cleanupRec -LiveSid $shapeSid
Assert-Equal $cdKeep.Decision 'keep' 'cleanup decision keeps the owned account when disable is not authorized'
$cdDisable = Test-D4CleanupAccountDecision -BootstrapState $cleanupRec -LiveSid $shapeSid -DisableWhenOwned
Assert-Equal $cdDisable.Decision 'disable' 'cleanup decision offers disable ONLY with the same-SID proof + authorization'

# ---- §95.5-D: namespace exemption uses the FULL shape -------------------------
Write-Host '== §95-D: namespace owned-task exemption =='
$nsOs = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$nsSch = New-D4FakeSchtasks -Oses $nsOs
$ownTaskDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$nsSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $ownTaskDef
$nsCfg = Copy-D4TestConfig @($rulePermit, $ruleBlockV6)
$nsCfg['share_account'] = 'um_share_d4'
$nsCfg['share_sid'] = $shapeSid
$nsCfg['logon_method'] = 'password-managed'
$nsCfg['runtime_source_dir'] = $rtDir
$nsCfg['runtime_files'] = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$invPw = New-D4ObjectInventory -Contract $contract -ClosureManifest $manifest -Config $nsCfg -PackageHash 'abcdef0123456789'
$ownSpec = @{ Name = [string]$contract['ShareTaskName']; IdentitySid = $shapeSid; LogonType = 'Password';
              Command = (Join-Path $contract['ExperimentRoot'] 'run-live.cmd'); TaskPath = '\'; Trigger = 'ONDEMAND' }
$freeOwn = Test-D4NamespaceFree -Inventory $invPw -Contract $contract -SchtasksRunner $nsSch.Runner -NetStatRunner $net.Runner -FileBoundary $nsOs.Boundary -OwnShareTask $ownSpec
Assert-Equal $freeOwn['Verdict'] 'free' 'the run-owned share task passes the FULL shape and is NOT foreign occupancy'
$cmdTag = '<Command>' + [regex]::Escape((Join-Path $contract['ExperimentRoot'] 'run-live.cmd')) + '</Command>'
$foreignDef = $ownTaskDef -replace $cmdTag, '<Command>C:\evil\other.cmd</Command>'
$nsSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $foreignDef
$freeForeignCmd = Test-D4NamespaceFree -Inventory $invPw -Contract $contract -SchtasksRunner $nsSch.Runner -NetStatRunner $net.Runner -FileBoundary $nsOs.Boundary -OwnShareTask $ownSpec
Assert-Equal $freeForeignCmd['Verdict'] 'occupied' 'same SID+Password with a DIFFERENT command stays a foreign occupant'
$nsSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $ownTaskDef
$freeForeignSid = Test-D4NamespaceFree -Inventory $invPw -Contract $contract -SchtasksRunner $nsSch.Runner -NetStatRunner $net.Runner -FileBoundary $nsOs.Boundary -OwnShareTask (@{ Name = [string]$contract['ShareTaskName']; IdentitySid = 'S-1-5-21-777-77-0000'; LogonType = 'Password'; Command = (Join-Path $contract['ExperimentRoot'] 'run-live.cmd'); TaskPath = '\'; Trigger = 'ONDEMAND' })
Assert-Equal $freeForeignSid['Verdict'] 'occupied' 'a same-name task with a DIFFERENT SID stays a foreign occupant'

# ===========================================================================
# §93/§95 ENTRY: T0 -> S0 -> S1 -> Cleanup over the password-managed run,
# plus the §95.4 recovery path (config write failure -> same-run resume)
# ===========================================================================
# the bootstrap record binds the STABLE closure-only package identity -
# the SAME value the real entry derives (closure manifest without the
# runtime manifest), computed here from the same reviewed inputs.
$pwBootstrapIdentity = Get-D4PackageHash -Contract $contract -ClosureManifest $manifest -SourceRoot $sourceRoot -RuntimeManifestJson ''

Write-Host '== §93 ENTRY: T0 bootstrap -> S0/S1/Cleanup password-managed run =='
$pwOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$pwSch = New-D4FakeSchtasks -Oses $pwOs
$pwAcct = New-D4FakeAccountRunner -TaskModel $pwSch.State['Tasks']
$pwNet = New-D4EmptyRunner -Tag 'net'
$pwProc = New-D4EmptyRunner -Tag 'proc'
$pwRules = New-D4FakeRuleStore -CreatedFacts @{}
$pwAcl = New-D4HarnessAclRunner -Oses $pwOs
$pwPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\plan-entry.json'
$pwRuleIds = @('44444444-4444-4444-8444-444444444444',
               '55555555-5555-4555-8555-555555555555',
               '66666666-6666-4666-8666-666666666666')
# the plan identity the ENTRY T0 binds = the canonical fingerprint of the
# plan fixture (the same slots, computed, never hand-written)
$pwPlanIdentityComputed = Get-D4BootstrapPlanIdentity -RunId $acctRunId `
    -AccountName 'um_share_d4' -TaskName ([string]$contract['ShareTaskName']) `
    -PythonHome 'C:\PythonExp' -RuntimeSourceDir $rtDir `
    -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap (Get-D4RuleRoleMap -OrderedIds $pwRuleIds)
Invoke-D4File -Boundary $pwOs.Boundary -Operation 'WriteText' -Arguments @($pwPlanPath,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$pwBootStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$pwEmitCfg = 'C:\ProgramData\UMShareExp-bootstrap\emitted-config.json'
$pwCfgReal = Join-Path $tmpBase 'pw-entry-config.json'
$pwSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $pwEmitCfg
    SchtasksRunner = $pwSch.Runner
    NetStatRunner = $pwNet.Runner
    ProcessEvidenceRunner = $pwProc.Runner
    FileBoundary = $pwOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $pwAcl.Runner
    RulesRunner = $pwRules.Runner
    AccountRunner = $pwAcct
    BootstrapStatePath = $pwBootStatePath
    BootstrapPlanPath = $pwPlanPath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
# §95.4: a missing -ConfigPath must cost ZERO account-create calls
$pwSplatNoCfg = $pwSplat.Clone()
$pwSplatNoCfg['ConfigPath'] = ''
Assert-Throws { & $entry -Stage 'T0' @pwSplatNoCfg } 'a missing config output path refuses BEFORE any account action' 'ConfigPath'
Assert-Equal $pwAcct.State['CreateCount'] 0 'missing -ConfigPath costs ZERO CreateAccount calls'
& $entry -Stage 'T0' @pwSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'T0 exits 0 (account + password-managed share task registered)'
Assert-Equal $pwAcct.State['CreateCount'] 1 'T0 created the account exactly once'
Assert-Equal $pwAcct.State['RegisteredCount'] 1 'T0 registered the share task exactly once'
Assert-True ($pwSch.State['Tasks'].ContainsKey($contract['ShareTaskName'].ToLowerInvariant())) 'T0 left the share task in the fake scheduler'
Assert-True ($pwSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()].Contains('<LogonType>Password</LogonType>')) 'T0 registered a Password-logon task'
Assert-True ($pwOs.State['Files'].ContainsKey($pwBootStatePath.ToLowerInvariant())) 'T0 wrote its bootstrap record OUTSIDE the experiment root (protected bootstrap dir)'
Assert-True ($pwOs.State['Files'].ContainsKey($pwEmitCfg.ToLowerInvariant())) 'T0 emitted the SID-bearing experiment config'
$pwRec = Read-D4BootstrapState -StatePath $pwBootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentityComputed -FileBoundary $pwOs.Boundary
Assert-Equal $pwRec.status 'task-registered' 'bootstrap record is task-registered after T0'
Assert-True ((@($pwAcct.State['Calls']) | Where-Object { $_ -match 'pw=' }).Count -eq 0) 'the ENTRY T0 call log NEVER carries a password value'
# transfer the emitted config to a REAL path for the S0/S1 entry reads
$emittedJson = $pwOs.State['Files'][$pwEmitCfg.ToLowerInvariant()]
[IO.File]::WriteAllText($pwCfgReal, $emittedJson, (New-Object System.Text.UTF8Encoding($false)))
# T0 recovery: the SAME rule ids come back (plan-fixed, never regenerated)
$emittedCfgObjPw = $emittedJson | ConvertFrom-Json
Assert-Equal ((@($emittedCfgObjPw.limit_rules) | ForEach-Object { [string]$_.id }) -join ',') ($pwRuleIds -join ',') 'the emitted config carries the PLAN-fixed rule ids (recovery-stable)'
# seed the fake rule store with matching review facts for the emitted ids
foreach ($rlPw in @($emittedCfgObjPw.limit_rules)) {
    if ([string]$rlPw.layer -eq 'connect-v4' -and [string]$rlPw.action -eq 'permit') {
        $factPw = 'layer=' + $layerV4 + ' action=0x1002 conds=3 appid=MATCH addr=2130706433 port=9100'
    } elseif ([string]$rlPw.layer -eq 'connect-v4' -and [string]$rlPw.action -eq 'block') {
        $factPw = 'layer=' + $layerV4 + ' action=0x1001 conds=1 appid=MATCH addr=NA port=NA'
    } else {
        $factPw = 'layer=' + $layerV6 + ' action=0x1001 conds=1 appid=MATCH addr=NA port=NA'
    }
    $pwRules.State['CreatedFacts'][([string]$rlPw.id).ToLowerInvariant()] = $factPw
}
$pwRules.State['Live'] = @{}
# S0 sees the run-owned share task as part of THIS run's namespace
$pwSplat['ConfigPath'] = $pwCfgReal
& $entry -Stage 'S0' @pwSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'S0 accepts the T0-registered share task (full-shape run-owned exemption)'
# S1 verifies the registered task, writes wrapper + definition, NO re-register
$pwSplat['BootstrapPlanPath'] = ''
& $entry -Stage 'S1' @pwSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'S1 completes the password-managed prepare (verify-only share task)'
Assert-Equal $pwAcct.State['RegisteredCount'] 1 'S1 NEVER re-registers the share task (no credential here)'
& $entry -Stage 'Cleanup' @pwSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'Cleanup completes the password-managed run'
Assert-True (-not $pwSch.State['Tasks'].ContainsKey($contract['ShareTaskName'].ToLowerInvariant())) 'the verified share task was deleted by identity-checked cleanup'
# ---- §95.4-C recovery: registration done, config write failed -> resume ------
Write-Host '== §95-C ENTRY: config emit failure then same-run resume =='
$pwOs2 = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$pwSch2 = New-D4FakeSchtasks -Oses $pwOs2
$pwAcct2 = New-D4FakeAccountRunner -TaskModel $pwSch2.State['Tasks']
$pwNet2 = New-D4EmptyRunner -Tag 'net'
$pwProc2 = New-D4EmptyRunner -Tag 'proc'
$pwRules2 = New-D4FakeRuleStore -CreatedFacts @{}
$pwAcl2 = New-D4HarnessAclRunner -Oses $pwOs2
$pwPlanPath2 = 'C:\ProgramData\UMShareExp-bootstrap\plan-entry2.json'
Invoke-D4File -Boundary $pwOs2.Boundary -Operation 'WriteText' -Arguments @($pwPlanPath2,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$pwBootStatePath2 = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$pwEmitCfg2 = 'C:\ProgramData\UMShareExp-bootstrap\emitted-config2.json'
$pwSplat2 = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $pwEmitCfg2
    SchtasksRunner = $pwSch2.Runner
    NetStatRunner = $pwNet2.Runner
    ProcessEvidenceRunner = $pwProc2.Runner
    FileBoundary = $pwOs2.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $pwAcl2.Runner
    RulesRunner = $pwRules2.Runner
    AccountRunner = $pwAcct2
    BootstrapStatePath = $pwBootStatePath2
    BootstrapPlanPath = $pwPlanPath2
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
# Wrap the fake boundary's WriteText: the FIRST config emission throws
# (a simulated write failure AFTER the account/task are registered - the
# §95.4-C recovery scenario). Record writes use a different path and pass.
$origWrite2 = $pwOs2.Boundary['WriteText']
$pwOs2.Boundary['WriteText'] = {
    param([string]$Path, [string]$Content)
    if ($Path -eq $pwEmitCfg2) { throw 'simulated config emit failure' }
    & $origWrite2 $Path $Content
}.GetNewClosure()
Assert-Throws { & $entry -Stage 'T0' @pwSplat2 } 'the first T0 registers but cannot emit (simulated write failure)' 'simulated config emit failure'
Assert-Equal $pwAcct2.State['CreateCount'] 1 'the failed-emission T0 created the account (registration side effects retained)'
Assert-Equal $pwAcct2.State['RegisteredCount'] 1 'the failed-emission T0 registered the task exactly once'
$pwRec2 = Read-D4BootstrapState -StatePath $pwBootStatePath2 -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentityComputed -FileBoundary $pwOs2.Boundary
Assert-Equal $pwRec2.status 'task-registered' 'the record stays task-registered (traceable), never silently reset'
Assert-True (-not $pwOs2.State['Files'].ContainsKey($pwEmitCfg2.ToLowerInvariant())) 'no config was emitted by the failed attempt'
# Restore the boundary and resume the SAME run: no re-create, no re-register.
$pwOs2.Boundary['WriteText'] = $origWrite2
& $entry -Stage 'T0' @pwSplat2 | Out-Null
Assert-Equal $LASTEXITCODE 0 'the SAME run resumes after the config failure'
Assert-Equal $pwAcct2.State['CreateCount'] 1 'the resume performs ZERO additional account-create calls'
Assert-Equal $pwAcct2.State['RegisteredCount'] 1 'the resume performs ZERO additional registrations (register count stays 1)'
$pwEmitObj2 = ($pwOs2.State['Files'][$pwEmitCfg2.ToLowerInvariant()]) | ConvertFrom-Json
Assert-Equal ((@($pwEmitObj2.limit_rules) | ForEach-Object { [string]$_.id }) -join ',') ($pwRuleIds -join ',') 'the resumed config carries the SAME plan-fixed rule ids'

# ---- §95.4-C: a DIFFERENT-content config file is never clobbered -------------
Write-Host '== §95-C: different config content is never clobbered =='
$pwOs3 = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$pwSch3 = New-D4FakeSchtasks -Oses $pwOs3
$pwAcct3 = New-D4FakeAccountRunner -TaskModel $pwSch3.State['Tasks']
$pwNet3 = New-D4EmptyRunner -Tag 'net'
$pwProc3 = New-D4EmptyRunner -Tag 'proc'
$pwRules3 = New-D4FakeRuleStore -CreatedFacts @{}
$pwAcl3 = New-D4HarnessAclRunner -Oses $pwOs3
$pwPlanPath3 = 'C:\ProgramData\UMShareExp-bootstrap\plan-entry3.json'
Invoke-D4File -Boundary $pwOs3.Boundary -Operation 'WriteText' -Arguments @($pwPlanPath3,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$pwBootStatePath3 = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$pwEmitCfg3 = 'C:\ProgramData\UMShareExp-bootstrap\emitted-config3.json'
Invoke-D4File -Boundary $pwOs3.Boundary -Operation 'WriteText' -Arguments @($pwEmitCfg3, '{"someone_elses": "config"}')
$pwSplat3 = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $pwEmitCfg3
    SchtasksRunner = $pwSch3.Runner
    NetStatRunner = $pwNet3.Runner
    ProcessEvidenceRunner = $pwProc3.Runner
    FileBoundary = $pwOs3.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $pwAcl3.Runner
    RulesRunner = $pwRules3.Runner
    AccountRunner = $pwAcct3
    BootstrapStatePath = $pwBootStatePath3
    BootstrapPlanPath = $pwPlanPath3
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
Assert-Throws { & $entry -Stage 'T0' @pwSplat3 } 'a foreign config at the output path is never clobbered (early precheck)' 'no clobber'
Assert-Equal ($pwOs3.State['Files'][$pwEmitCfg3.ToLowerInvariant()]) '{"someone_elses": "config"}' 'the foreign config bytes are UNCHANGED'

# ---- §95.6: fail-closed guards (missing runner / guard / operator) ------------
Write-Host '== §95: fail-closed harness guards =='
$fcSplat = $pwSplat.Clone()
$fcSplat['AccountRunner'] = $null
Assert-Throws { & $entry -Stage 'S0' @fcSplat } 'password-managed harness without the account runner fails closed' 'account runner'
$fcT0 = $pwSplat.Clone()
$fcT0['AccountRunner'] = $null
$fcT0['ConfigPath'] = $pwEmitCfg
Assert-Throws { & $entry -Stage 'T0' @fcT0 } 'T0 harness without the account runner fails closed' 'account/credential runner'
$fcG = $pwSplat.Clone()
$fcG['BootstrapStoreGuard'] = $null
$fcG['ConfigPath'] = $pwEmitCfg
Assert-Throws { & $entry -Stage 'T0' @fcG } 'T0 harness without the protected-store guard fails closed' 'protected-store guard'
$fcOp = $pwSplat.Clone()
$fcOp['BootstrapOperatorAccount'] = ''
$fcOp['ConfigPath'] = $pwEmitCfg
$fcOp['BootstrapPlanPath'] = $pwPlanPath
Assert-Throws { & $entry -Stage 'T0' @fcOp } 'T0 harness without the injected operator identity fails closed (real identity never leaks offline)' 'operator identity'

# ---- §95-R: review-round targeted additions ------------------------------------
Write-Host '== §95-R: a bootstrap-state write interruption stays traceable =='
# The 'created' save fails mid-run: the on-disk record stays at its LAST
# traceable status (creation-intent), the run stops, and NOTHING is
# registered - the record is the trace; a new run id is the only way out.
$rwOs = New-D4InMemoryOs -Root $bootRoot
$rwSch = New-D4FakeSchtasks -Oses $rwOs
$rwAcct = New-D4FakeAccountRunner -TaskModel $rwSch.State['Tasks']
$rwOrigWrite = $rwOs.Boundary['WriteText']
$script:rwStateWrites = 0
$rwOs.Boundary['WriteText'] = {
    param([string]$Path, [string]$Content)
    if ($Path -eq $bootStatePath) {
        $script:rwStateWrites++
        if ($script:rwStateWrites -ge 2) { throw 'simulated bootstrap-state write interruption' }
    }
    & $rwOrigWrite $Path $Content
}.GetNewClosure()
Assert-Throws { Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $rwAcct -FileBoundary $rwOs.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity } 'a state-write interruption stops the bootstrap (traceable record)' 'simulated bootstrap-state write interruption'
Assert-Equal $rwAcct.State['RegisteredCount'] 0 'a state-write interruption registers NOTHING'
$rwRaw = [string](Invoke-D4File -Boundary $rwOs.Boundary -Operation 'ReadText' -Arguments @($bootStatePath))
Assert-True ($rwRaw.Contains('creation-intent')) 'the on-disk record stays at the LAST traceable status (creation-intent)'

Write-Host '== §95-R: a config SID disagreeing with the record grants NO exemption =='
$misOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$misSch = New-D4FakeSchtasks -Oses $misOs
$misNet = New-D4EmptyRunner -Tag 'net'
$misProc = New-D4EmptyRunner -Tag 'proc'
$misRules = New-D4FakeRuleStore -CreatedFacts @{}
$misAcl = New-D4HarnessAclRunner -Oses $misOs
$misCfgObj = Copy-D4TestConfig @($rulePermit, $ruleBlockV4, $ruleBlockV6)
$misCfgObj['run_id'] = $acctRunId
$misCfgObj['share_account'] = 'um_share_d4'
$misCfgObj['share_sid'] = 'S-1-5-21-777-77-0000'
$misCfgObj['logon_method'] = 'password-managed'
$misCfgObj['runtime_source_dir'] = $rtDir
$misCfgObj['runtime_files'] = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$misCfgReal = Join-Path $tmpBase 'pw-mismatch-config.json'
[IO.File]::WriteAllText($misCfgReal, ($misCfgObj | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
# the record+task carry SID X; the CONFIG carries a DIFFERENT share_sid
$misRec = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentity
$misRec.status = 'task-registered'
$misRec.created_sid = $shapeSid
Invoke-D4File -Boundary $misOs.Boundary -Operation 'WriteText' -Arguments @($pwBootStatePath, ($misRec | ConvertTo-Json -Depth 4))
$misDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$misSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $misDef
$misSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $misCfgReal
    SchtasksRunner = $misSch.Runner
    NetStatRunner = $misNet.Runner
    ProcessEvidenceRunner = $misProc.Runner
    FileBoundary = $misOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = (New-D4HarnessAclRunner -Oses $misOs).Runner
    RulesRunner = $misRules.Runner
    AccountRunner = (New-D4FakeAccountRunner -TaskModel $misSch.State['Tasks'] -SeedSid $shapeSid)
    BootstrapStatePath = $pwBootStatePath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
}
Assert-Throws { & $entry -Stage 'S0' @misSplat } 'a config SID that disagrees with the record grants NO exemption (task stays occupied)' 'S0 inventory verdict=occupied'

Write-Host '== §95-R: E1 verifies task identity BEFORE any /Run =='
$osE1 = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schE1 = New-D4FakeSchtasks -Oses $osE1
$aclE1 = New-D4HarnessAclRunner -Oses $osE1
$storeE1 = New-D4FakeRuleStore -CreatedFacts $createdFacts
$probeE1 = New-D4FakeProbe -Oses $osE1 -Contract $contract
$pairE1 = New-D4LifecycleNetPair -SchRunner $schE1.Runner -ExperimentRoot $contract['ExperimentRoot']
$splatE1 = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pairE1.Schtasks `
    -Net $pairE1.Net -Proc $pairE1.Proc -Probe $probeE1.Runner `
    -Rules $storeE1.Runner -Os $osE1 -Acl $aclE1.Runner -Installer $brokerInstaller -Store $storeFactory
& $entry -Stage 'S0' @splatE1 | Out-Null
& $entry -Stage 'S1' @splatE1 | Out-Null
# Tamper the BROKER definition AFTER S1 (the realistic window): same
# principal, WRONG command. The E1 pre-start verification must refuse with
# ZERO /Run calls (the identity gate precedes every start).
$brokerDefKey = ($contract['ExperimentRoot'] + '\broker\task-definition.xml').ToLowerInvariant()
$cmdTagE1 = '<Command>' + [regex]::Escape('C:\ProgramData\UMShareExp-broker\run-broker.cmd') + '</Command>'
$tamperedBrokerXml = $schE1.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()]
$tamperedBrokerXml = $tamperedBrokerXml -replace $cmdTagE1, '<Command>C:\evil\other-broker.cmd</Command>'
$schE1.State['Tasks'][$contract['BrokerTaskName'].ToLowerInvariant()] = $tamperedBrokerXml
$runsBeforeE1 = (@($schE1.State['Calls']) | Where-Object { $_ -match '/Run' }).Count
Assert-Throws { & $entry -Stage 'E1' @splatE1 } 'E1 refuses a tampered task definition BEFORE any start' 'identity NOT confirmed before start'
Assert-Equal ((@($schE1.State['Calls']) | Where-Object { $_ -match '/Run' }).Count) $runsBeforeE1 'the refused E1 performed ZERO /Run calls'

# ---- E1 closure offline: telemetry retention + QUALIFIED cleanup (W2 run21) ---
Write-Host '== E1 closure: R83 telemetry retention lets the qualified cleanup pass =='
$osE1c = New-D4InMemoryOs -Root $contract['ExperimentRoot']
$schE1c = New-D4FakeSchtasks -Oses $osE1c
$aclE1c = New-D4HarnessAclRunner -Oses $osE1c
$storeE1c = New-D4FakeRuleStore -CreatedFacts $createdFacts
$probeE1c = New-D4FakeProbe -Oses $osE1c -Contract $contract
$pairE1c = New-D4LifecycleNetPair -SchRunner $schE1c.Runner -ExperimentRoot $contract['ExperimentRoot']
$splatE1c = New-D4EntrySplat -RunId $runId -ConfigPath $entryCfgPath -Schtasks $pairE1c.Schtasks `
    -Net $pairE1c.Net -Proc $pairE1c.Proc -Probe $probeE1c.Runner `
    -Rules $storeE1c.Runner -Os $osE1c -Acl $aclE1c.Runner -Installer $brokerInstaller -Store $storeFactory
foreach ($s in @('S0', 'S1', 'E1')) {
    & $entry -Stage $s @splatE1c | Out-Null
    Assert-Equal $LASTEXITCODE 0 ('E1-closure stage ' + $s + ' exits 0')
}
$ledE1cPath = Get-D4IntentLedgerPath -Contract $contract -PackageHash $pkg -RunId $runId
$ledE1c = Invoke-D4File -Boundary $osE1c.Boundary -Operation 'ReadText' -Arguments @($ledE1cPath) | ConvertFrom-Json
foreach ($rel in @('broker\broker-status.json', 'run\live\guard-status.json', 'run\live\instance-registration.json')) {
    $e1c = @($ledE1c.entries | Where-Object { $_.path -eq (Join-Path $contract['ExperimentRoot'] $rel) })[0]
    Assert-True ($null -ne $e1c) ('the telemetry artifact is ledger-planned: ' + $rel)
    Assert-True ([string]$e1c.status -eq 'retained') ('E1 marks the runtime telemetry RETAINED: ' + $rel)
    Assert-True ([string]$e1c.retained_reason -like '*E1*') ('the retention reason records the E1 writing stage: ' + $rel)
}
$cleanupE1cOut = @(& $entry -Stage 'Cleanup' @splatE1c 2>&1)
Assert-Equal $LASTEXITCODE 0 'the QUALIFIED E1-closure cleanup passes (no failed-clean dirs, no planned takeover)'
Assert-True (($cleanupE1cOut -join "`n") -match 'retained') 'the E1-closure cleanup reports the retained telemetry containers'

# ---- R97-A: task path wiring + explicit logon context at the bootstrap ----
Write-Host '== §97-A: thin real runner register/query shape (fake cmdlet interceptor) =='
$script:r97RegStore = @{}
$script:r97RegCalls = New-Object System.Collections.Generic.List[hashtable]
function script:Register-ScheduledTask {
    param([string]$Xml, [string]$TaskName, [string]$TaskPath, [string]$User, [string]$Password)
    # Folder+name model: the store key is the FOLDER + name; the credential
    # value is NEVER recorded (length only).
    $script:r97RegStore[([string]$TaskPath + '\' + [string]$TaskName).ToLowerInvariant()] = [string]$Xml
    $script:r97RegCalls.Add(@{ name = [string]$TaskName; path = [string]$TaskPath;
                               user = [string]$User; pwlen = [string]$Password.Length })
    return $null
}
function script:Get-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath)
    # BOTH call shapes: the new queryTask ENUMERATES the folder (-TaskPath
    # only) and the exported-task lookup passes -TaskName too
    if ($TaskName) {
        if ($script:r97RegStore.ContainsKey(([string]$TaskPath + '\' + [string]$TaskName).ToLowerInvariant())) {
            return [pscustomobject]@{ TaskName = $TaskName; TaskPath = $TaskPath }
        }
        return $null
    }
    return [object[]]($script:r97RegStore.Keys | ForEach-Object {
        $k = [string]$_
        $n = $k.Substring($k.LastIndexOf('\') + 1)
        [pscustomobject]@{ TaskName = $n; TaskPath = '\' }
    })
}
function script:Export-ScheduledTask {
    # the REAL cmdlet consumes pipeline input - bind via $input, never a
    # plain param() (which never binds from a pipe)
    $t = @($input)[0]
    return $script:r97RegStore[([string]$t.TaskPath + '\' + [string]$t.TaskName).ToLowerInvariant()]
}
$r97Runner = New-D4AccountRunner
$r97Reg = & $r97Runner['RegisterShareTask'] 'umshare-share-exp' '\' $shapeOwned 'um_share_d4' 'shape-test-credential' @{}
Assert-True ([bool]$r97Reg['ok']) 'the thin real runner registers through the fake cmdlet shim'
Assert-Equal $script:r97RegCalls[0]['path'] '\' 'Register-ScheduledTask receives TaskPath as the ROOT FOLDER (§97.2)'
Assert-Equal $script:r97RegCalls[0]['name'] 'umshare-share-exp' 'Register-ScheduledTask receives the task NAME separately'
Assert-True ($script:r97RegCalls[0]['pwlen'] -gt 0) 'the credential reached the cmdlet parameter (length only, value never recorded)'
$r97Q = & $r97Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-True ([bool]$r97Q['present']) 'the query addresses the SAME registered object (folder+name)'
Assert-True ([string]$r97Q['xml'] -eq [string]$shapeOwned) 'the queried definition is the registered definition (same object)'
Remove-Item function:Register-ScheduledTask, function:Get-ScheduledTask, function:Export-ScheduledTask -ErrorAction SilentlyContinue

Write-Host '== §97-A: S4U / HighestAvailable injected at the REAL bootstrap call points refuse =='
# S4U override: the register 'succeeds' but the exported definition is S4U;
# the post-register read-back must refuse (logon context enforced).
$bOsA = New-D4InMemoryOs -Root $bootRoot
$bSchA = New-D4FakeSchtasks -Oses $bOsA
$bAcctA = New-D4FakeAccountRunner -TaskModel $bSchA.State['Tasks']
$bAcctA.State['RegisterS4UOverride'] = $true
$rA97 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $bAcctA -FileBoundary $bOsA.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rA97.Ok -and $rA97.Refused -eq 'unconfirmed') 'an S4U-registered task fails the post-register read-back (logon context enforced at the real call point)'
Assert-Equal $bAcctA.State['RegisteredCount'] 1 'the S4U registration landed (the refusal comes from the read-back verification)'
# HighestAvailable override: same enforcement for the implied RunLevel.
$bOsH = New-D4InMemoryOs -Root $bootRoot
$bSchH = New-D4FakeSchtasks -Oses $bOsH
$bAcctH = New-D4FakeAccountRunner -TaskModel $bSchH.State['Tasks']
$bAcctH.State['RegisterHighestOverride'] = $true
$rH97 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $bAcctH -FileBoundary $bOsH.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rH97.Ok -and $rH97.Refused -eq 'unconfirmed') 'a HighestAvailable-registered Password task fails the post-register read-back (RunLevel enforced)'
# Reuse path: a task-registered record + a live S4U task under the SAME
# recorded SID -> the idempotent branch refuses (never adopts).
$bOsR = New-D4InMemoryOs -Root $bootRoot
$bSchR = New-D4FakeSchtasks -Oses $bOsR
$bAcctR = New-D4FakeAccountRunner -TaskModel $bSchR.State['Tasks'] -SeedSid $shapeSid
$s4uLiveXml = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath $shapeCmd
$bSchR.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $s4uLiveXml
$recR97 = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$recR97.status = 'task-registered'
$recR97.created_sid = $shapeSid
Invoke-D4File -Boundary $bOsR.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($recR97 | ConvertTo-Json -Depth 4))
$rR97 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $bAcctR -FileBoundary $bOsR.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $rR97.Ok) 'a live S4U task under the recorded SID is NOT reused (full shape incl. logon context enforced through the idempotent branch)'

# ---- R97-B: the read-only trust gate (owner/DACL/reparse/leaf) ----
$d97RightsFullControl = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::FullControl -band 0xFFFFFFFF)
$d97RightsReadExec = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::ReadAndExecute -band 0xFFFFFFFF)
$d97RightsDelete = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::Delete -band 0xFFFFFFFF)
$d97RightsDeleteChild = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -band 0xFFFFFFFF)
$d97RightsGenericAll = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::GenericAll -band 0xFFFFFFFF)
$d97RightsChangePerm = [uint64]([int64][System.Security.AccessControl.FileSystemRights]::ChangePermissions -band 0xFFFFFFFF)
$d97ForeignSid = 'S-1-5-21-777-77-4242'
function New-D4TrustedDescriptor {
    # §101.3: the trusted root descriptor is a REAL in-memory .NET
    # FileSecurity (SYSTEM owner + SYSTEM/Admin FullControl) - the
    # positive paths verify on the REAL type, never on a fabricated shape
    return (New-D4RealInMemoryFileSecurity -OwnerSid 'S-1-5-18' -Rules @(
        @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
        @{ identitySid = 'S-1-5-32-544'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl }))
}

Write-Host '== §97-B: trust gate cases =='
$bootRootLow = 'c:\programdata\umshareexp-bootstrap'
# Case 1: a semantically-valid record + a FOREIGN explicit write ACE on the leaf.
$tgDesc1 = @{
    $bootRootLow = (New-D4TrustedDescriptor)
    $bootStatePath.ToLowerInvariant() = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = $d97RightsFullControl },
        @{ identitySid = $d97ForeignSid; type = 'Allow'; rights = $d97RightsFullControl }))
}
$tgProbe1 = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -DescriptorByPath $tgDesc1).Edge
$tgOs1 = New-D4InMemoryOs -Root $bootRoot
$rec1 = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$rec1.status = 'task-registered'
$rec1.created_sid = $shapeSid
Invoke-D4File -Boundary $tgOs1.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($rec1 | ConvertTo-Json -Depth 4))
$tg1 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner (New-D4FakeAccountRunner -TaskModel @{}) `
    -FileBoundary $tgOs1.Boundary -StoreGuard (New-D4FakeStoreGuard).Guard `
    -StoreProbe $noReparseProbe -AclProbe $tgProbe1 -PlanIdentity $pwPlanIdentity
Assert-True (-not $tg1.Ok) 'a foreign write ACE on the record leaf refuses the whole bootstrap'
Assert-True ($tg1.Reason -match 'leaf is NOT trusted|holds a write') ('the refusal names the leaf trust failure (reason=' + [string]$tg1.Reason + ')')
# Case 2: safe parent + individually writable leaf (the leaf alone is open).
$tgDesc2 = @{
    $bootRootLow = (New-D4TrustedDescriptor)
    $bootStatePath.ToLowerInvariant() = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = $d97RightsFullControl },
        @{ identitySid = 'S-1-5-32-545'; type = 'Allow'; rights = $d97RightsDeleteChild }))
}
$tgProbe2 = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -DescriptorByPath $tgDesc2).Edge
$tgOs2 = New-D4InMemoryOs -Root $bootRoot
Invoke-D4File -Boundary $tgOs2.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($rec1 | ConvertTo-Json -Depth 4))
$tg2 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner (New-D4FakeAccountRunner -TaskModel @{}) `
    -FileBoundary $tgOs2.Boundary -StoreGuard (New-D4FakeStoreGuard).Guard `
    -StoreProbe $noReparseProbe -AclProbe $tgProbe2 -PlanIdentity $pwPlanIdentity
Assert-True (-not $tg2.Ok) 'a writable-by-Users record leaf refuses even with a safe parent'
# Case 3: the record leaf is a REPARSE point.
$tgProbe3 = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -AlwaysTrusted).Edge
$tgOs3 = New-D4InMemoryOs -Root $bootRoot
Invoke-D4File -Boundary $tgOs3.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($rec1 | ConvertTo-Json -Depth 4))
$leafReparseProbe = {
    param([string]$Path)
    if ([string]$Path -eq $bootStatePath) { return @{ Attributes = [IO.FileAttributes]::ReparsePoint } }
    return $null
}.GetNewClosure()
$tg3 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner (New-D4FakeAccountRunner -TaskModel @{}) `
    -FileBoundary $tgOs3.Boundary -StoreGuard (New-D4FakeStoreGuard).Guard `
    -StoreProbe $leafReparseProbe -AclProbe $tgProbe3 -PlanIdentity $pwPlanIdentity
Assert-True (-not $tg3.Ok) 'a reparse-point record leaf refuses (never redirected)'
# Case 4: the ACL query itself fails -> untrusted (never absence).
$tgProbe4 = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -Fails).Edge
$tgOs4 = New-D4InMemoryOs -Root $bootRoot
$tg4 = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner (New-D4FakeAccountRunner -TaskModel @{}) `
    -FileBoundary $tgOs4.Boundary -StoreGuard (New-D4FakeStoreGuard).Guard `
    -StoreProbe $noReparseProbe -AclProbe $tgProbe4 -PlanIdentity $pwPlanIdentity
Assert-True (-not $tg4.Ok) 'an ACL query failure refuses the bootstrap (unconfirmed, never absence)'
# Positive: a fully trusted store + valid record -> the read-only path ok,
# and the CREATE guard is never consulted (the store already exists).
$tgProbeOk = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -AlwaysTrusted).Edge
$tgOsOk = New-D4InMemoryOs -Root $bootRoot
Invoke-D4File -Boundary $tgOsOk.Boundary -Operation 'MakeDir' -Arguments @($bootRoot)
Invoke-D4File -Boundary $tgOsOk.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($rec1 | ConvertTo-Json -Depth 4))
$tgGuardOk = New-D4FakeStoreGuard
    # the idempotent read-back needs the registered task to EXIST matching
    # the record - seed the fake scheduler with the recorded SID's task
    $tgSchOk = New-D4FakeSchtasks -Oses $tgOsOk
    $tgOkDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
    $tgSchOk.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $tgOkDef
$tgOk = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner (New-D4FakeAccountRunner -TaskModel $tgSchOk.State['Tasks'] -SeedSid $shapeSid) `
    -FileBoundary $tgOsOk.Boundary -StoreGuard $tgGuardOk.Guard `
    -StoreProbe $noReparseProbe -AclProbe $tgProbeOk -PlanIdentity $pwPlanIdentity
Assert-True ([bool]$tgOk.Ok) ('a trusted store with a valid record reads back through the read-only path (refused=' + [string]$tgOk.Refused + ' reason=' + [string]$tgOk.Reason + ')')
Assert-True ((@($tgGuardOk.State['Calls']) | Where-Object { $_ -match 'store guard' }).Count -eq 0) 'an EXISTING trusted store takes the READ-ONLY path (the create guard is never consulted)'

Write-Host '== §97-B: the REAL guard DECISION (existing store is never modified) =='
$rgNative = New-D4EmptyRunner -Tag 'icacls'
$rgBoundary = New-D4InMemoryOs -Root $bootRoot
Invoke-D4File -Boundary $rgBoundary.Boundary -Operation 'MakeDir' -Arguments @($bootRoot)
$rgUntrusted = New-D4FakeGetAclEdge -DescriptorByPath @{
    $bootRootLow = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-21-777-77-4242' -Rules @(
        @{ identitySid = 'S-1-5-21-777-77-4242'; type = 'Allow'; rights = $d97RightsFullControl }))
}
$rgGuard = New-D4RealBootstrapStoreGuard -RootDir $bootRoot -NativeRunner $rgNative.Runner `
    -AclProbe (New-D4RealAclProbe -GetAclEdge $rgUntrusted.Edge) -StoreProbe $noReparseProbe -FileBoundary $rgBoundary.Boundary
$rgResult = & $rgGuard $bootRoot @{}
Assert-True (-not [bool]$rgResult['ok']) 'the real guard REFUSES an existing untrusted store'
Assert-True ($rgResult['error'] -match 'NOT trusted') 'the refusal names the trust failure'
Assert-True ((@($rgNative.State['Calls']) | Where-Object { $_ -match 'icacls' }).Count -eq 0) 'an existing untrusted store receives ZERO icacls calls (zero modification)'
$rgTrustedProbe = New-D4FakeGetAclEdge -AlwaysTrusted
$rgGuardT = New-D4RealBootstrapStoreGuard -RootDir $bootRoot -NativeRunner (New-D4EmptyRunner -Tag 'icacls2').Runner `
    -AclProbe (New-D4RealAclProbe -GetAclEdge $rgTrustedProbe.Edge) -StoreProbe $noReparseProbe -FileBoundary $rgBoundary.Boundary
$rgResultT = & $rgGuardT $bootRoot @{}
Assert-True ([bool]$rgResultT['ok']) 'an existing TRUSTED store passes the read-only verification'

Write-Host '== §97-B ENTRY: an untrusted store grants NO run-owned exemption =='
$utOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$utSch = New-D4FakeSchtasks -Oses $utOs
$utNet = New-D4EmptyRunner -Tag 'net'
$utProc = New-D4EmptyRunner -Tag 'proc'
$utRules = New-D4FakeRuleStore -CreatedFacts @{}
$utAcl = New-D4HarnessAclRunner -Oses $utOs
$utCfgObj = Copy-D4TestConfig @($rulePermit, $ruleBlockV4, $ruleBlockV6)
$utCfgObj['run_id'] = $acctRunId
$utCfgObj['share_account'] = 'um_share_d4'
$utCfgObj['share_sid'] = $shapeSid
$utCfgObj['logon_method'] = 'password-managed'
$utCfgObj['runtime_source_dir'] = $rtDir
$utCfgObj['runtime_files'] = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$utCfgReal = Join-Path $tmpBase 'pw-untrusted-config.json'
[IO.File]::WriteAllText($utCfgReal, ($utCfgObj | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$utRec = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentity
$utRec.status = 'task-registered'
$utRec.created_sid = $shapeSid
Invoke-D4File -Boundary $utOs.Boundary -Operation 'WriteText' -Arguments @($pwBootStatePath, ($utRec | ConvertTo-Json -Depth 4))
$utDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$utSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $utDef
# the LEAF carries a foreign write ACE - the record itself is semantically valid
$utRows = @{
    $bootRootLow = (New-D4TrustedDescriptor)
    $pwBootStatePath.ToLowerInvariant() = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = $d97RightsFullControl },
        @{ identitySid = $d97ForeignSid; type = 'Allow'; rights = $d97RightsFullControl }))
}
$utSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $utCfgReal
    SchtasksRunner = $utSch.Runner
    NetStatRunner = $utNet.Runner
    ProcessEvidenceRunner = $utProc.Runner
    FileBoundary = $utOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $utAcl.Runner
    RulesRunner = $utRules.Runner
    AccountRunner = (New-D4FakeAccountRunner -TaskModel $utSch.State['Tasks'] -SeedSid $shapeSid)
    BootstrapStatePath = $pwBootStatePath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = (New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -DescriptorByPath $utRows).Edge)
}
Assert-Throws { & $entry -Stage 'S0' @utSplat } 'an untrusted bootstrap store grants NO run-owned exemption (task stays occupied)' 'S0 inventory verdict=occupied'

# ---- R97-C: dual identity binding (code identity + immutable plan) ----
Write-Host '== §97-C: the bootstrap module is INSIDE the code identity =='
$srcClone = Join-Path $tmpBase ('src-clone-' + [IO.Path]::GetRandomFileName())
foreach ($rel in (@($contract['ModuleClosure']) + @('um-share-deploy\execute-d4-experiment-lib.ps1',
        'um-share-deploy\execute-d4-experiment.ps1', 'um-share-deploy\install-share-broker-lib.ps1',
        'um-share-deploy\install-share-broker.ps1', 'um-share-deploy\share_guard.py',
        'chat2api\share_entry.py'))) {
    $dst = Join-Path $srcClone $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath (Join-Path $sourceRoot $rel) -Destination $dst
}
$cloneBoundary = New-D4RealFileBoundary
$cloneManifest = Get-D4SourceClosureManifest -Contract $contract -SourceRoot $srcClone -FileBoundary $cloneBoundary
$cloneHashPristine = Get-D4PackageHash -Contract $contract -ClosureManifest $cloneManifest -SourceRoot $srcClone -RuntimeManifestJson ''
$modCopy = Join-Path $srcClone 'um-share-deploy\d4-account-bootstrap.ps1'
[IO.File]::WriteAllText($modCopy, ([string][IO.File]::ReadAllText($modCopy)) + "`n# isolated-byte-change", (New-Object System.Text.UTF8Encoding($false)))
$cloneManifest2 = Get-D4SourceClosureManifest -Contract $contract -SourceRoot $srcClone -FileBoundary $cloneBoundary
$cloneHashTampered = Get-D4PackageHash -Contract $contract -ClosureManifest $cloneManifest2 -SourceRoot $srcClone -RuntimeManifestJson ''
Assert-True ($cloneHashPristine -ne $cloneHashTampered) 'changing ONLY the bootstrap module bytes changes the code identity (§97.4)'

Write-Host '== §97-C: a changed plan refuses the same run before any side effect =='
$pcOs = New-D4InMemoryOs -Root $bootRoot
$pcSch = New-D4FakeSchtasks -Oses $pcOs
$pcRec = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity
$pcRec.status = 'task-registered'
$pcRec.created_sid = $shapeSid
Invoke-D4File -Boundary $pcOs.Boundary -Operation 'WriteText' -Arguments @($bootStatePath, ($pcRec | ConvertTo-Json -Depth 4))
$pcBefore = [string](Invoke-D4File -Boundary $pcOs.Boundary -Operation 'ReadText' -Arguments @($bootStatePath))
$pcAcct = New-D4FakeAccountRunner -TaskModel $pcSch.State['Tasks'] -SeedSid $shapeSid
$pc = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $pcAcct -FileBoundary $pcOs.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity 'changed-plan-0000000000000000000000000000000000000000000000000000000001'
Assert-True (-not $pc.Ok) 'a changed plan refuses the same run (code identity and plan identity are separate kinds)'
Assert-Equal $pcAcct.State['CreateCount'] 0 'a plan change costs ZERO account-create calls'
Assert-Equal $pcAcct.State['RegisteredCount'] 0 'a plan change costs ZERO register calls'
Assert-True ([string](Invoke-D4File -Boundary $pcOs.Boundary -Operation 'ReadText' -Arguments @($bootStatePath)) -eq $pcBefore) 'the record bytes are UNCHANGED (no unconfirmed overwrite, still traceable)'
# through the REAL T0 path: register+config-write-fail, then a MODIFIED plan re-entry.
$mpOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$mpSch = New-D4FakeSchtasks -Oses $mpOs
$mpAcct = New-D4FakeAccountRunner -TaskModel $mpSch.State['Tasks']
$mpPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\plan-mod.json'
Invoke-D4File -Boundary $mpOs.Boundary -Operation 'WriteText' -Arguments @($mpPlanPath,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$mpBootStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$mpEmitCfg = 'C:\ProgramData\UMShareExp-bootstrap\emitted-config-mp.json'
$mpSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $mpEmitCfg
    SchtasksRunner = $mpSch.Runner
    NetStatRunner = (New-D4EmptyRunner -Tag 'net').Runner
    ProcessEvidenceRunner = (New-D4EmptyRunner -Tag 'proc').Runner
    FileBoundary = $mpOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = (New-D4HarnessAclRunner -Oses $mpOs).Runner
    RulesRunner = (New-D4FakeRuleStore -CreatedFacts @{}).Runner
    AccountRunner = $mpAcct
    BootstrapStatePath = $mpBootStatePath
    BootstrapPlanPath = $mpPlanPath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
$mpOrigWrite = $mpOs.Boundary['WriteText']
$mpOs.Boundary['WriteText'] = {
    param([string]$Path, [string]$Content)
    if ($Path -eq $mpEmitCfg) { throw 'simulated config emit failure' }
    & $mpOrigWrite $Path $Content
}.GetNewClosure()
Assert-Throws { & $entry -Stage 'T0' @mpSplat } 'the first T0 registers but cannot emit' 'simulated config emit failure'
Assert-Equal $mpAcct.State['RegisteredCount'] 1 'the first T0 registered the task exactly once'
$mpPlanModified = 'C:\ProgramData\UMShareExp-bootstrap\plan-mod-modified.json'
Invoke-D4File -Boundary $mpOs.Boundary -Operation 'WriteText' -Arguments @($mpPlanModified,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$mpSplat['BootstrapPlanPath'] = $mpPlanModified
Assert-Throws { & $entry -Stage 'T0' @mpSplat } 'a MODIFIED plan refuses the same-run recovery' 'without this run''s trusted record'
Assert-Equal $mpAcct.State['CreateCount'] 1 'the modified plan costs ZERO additional create calls'
Assert-Equal $mpAcct.State['RegisteredCount'] 1 'the modified plan costs ZERO additional register calls'
Assert-True (-not $mpOs.State['Files'].ContainsKey($mpEmitCfg.ToLowerInvariant())) 'the modified plan emits NO config'
$mpOs.Boundary['WriteText'] = $mpOrigWrite
$mpSplat['BootstrapPlanPath'] = $mpPlanPath
& $entry -Stage 'T0' @mpSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'the unchanged plan recovers the same run'
Assert-Equal $mpAcct.State['CreateCount'] 1 'the unchanged-plan resume performs ZERO additional create calls'

# ---- R97-D: SID-addressed disable + group-by-SID + SID persisted first ----
Write-Host '== §97-D: the disable boundary is SID-addressed (mismatched/unknown SID = zero disable) =='
$script:d97UserBySid = @{}
$script:d97DisableCalls = New-Object System.Collections.Generic.List[string]
$script:d97GetUserArgs = New-Object System.Collections.Generic.List[string]
function script:Get-LocalUser {
    param([string]$Name, [string]$SID)
    $script:d97GetUserArgs.Add('sid=' + [string]$SID + ' name=' + [string]$Name)
    if ([string]$SID) {
        if ($script:d97UserBySid.ContainsKey([string]$SID)) { return $script:d97UserBySid[[string]$SID] }
        throw ('user not found by SID: ' + [string]$SID)
    }
    if ([string]$Name) {
        if ([string]$Name -eq 'um_share_d4') { return [pscustomobject]@{ SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-1006' }; Enabled = $true } }
        throw ('user not found by name: ' + [string]$Name)
    }
    # ENUMERATION shape (no -Name/-SID): the §107.2 QueryAccount
    # enumerates the collection and filters by exact name itself
    return [object[]]@([pscustomobject]@{ Name = 'um_share_d4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-1006' }; Enabled = $true })
}
function script:Disable-LocalUser {
    param([string]$SID)
    $script:d97DisableCalls.Add([string]$SID)
    if ($script:d97UserBySid.ContainsKey([string]$SID)) { $script:d97UserBySid[[string]$SID]['Enabled'] = $false }
    return $null
}
function script:Get-LocalGroupMember {
    param([string]$SID)
    $script:d97GroupProbeArgs.Add([string]$SID)
    return @()
}
$script:d97GroupProbeArgs = New-Object System.Collections.Generic.List[string]
$d97Runner = New-D4AccountRunner
$d97SidMatch = 'S-1-5-21-777-77-5555'
$script:d97UserBySid[$d97SidMatch] = @{ SID = [pscustomobject]@{ Value = $d97SidMatch }; Enabled = $true }
$d97Ok = & $d97Runner['DisableAccount'] $d97SidMatch @{}
Assert-Equal ([string]$d97Ok['ok']) 'True' ('the disable boundary disables the recorded SID (result=' + ($d97Ok | ConvertTo-Json -Compress) + ')')
Assert-Equal ((@($script:d97DisableCalls) | Where-Object { $_ -eq $d97SidMatch }).Count) 1 'exactly ONE disable addressed to the recorded SID'
Assert-True (-not [bool]$script:d97UserBySid[$d97SidMatch]['Enabled']) 'the post-verify sees Enabled=false for the SAME SID'
$d97Miss = & $d97Runner['DisableAccount'] 'S-1-5-21-777-77-0000' @{}
Assert-True (-not [bool]$d97Miss['ok']) 'a SID that addresses NO live account refuses the disable (pre-prove fails)'
Assert-Equal ((@($script:d97DisableCalls) | Where-Object { $_ -eq 'S-1-5-21-777-77-0000' }).Count) 0 'an unknown SID receives ZERO disable calls'
$d97Q = & $d97Runner['QueryAccount'] 'um_share_d4' @{}
Assert-True ((@($script:d97GroupProbeArgs) | Where-Object { $_ -eq 'S-1-5-32-544' }).Count -ge 1) 'the admin probe queries the group BY SID S-1-5-32-544 (never a localized name)'
Remove-Item function:Get-LocalUser, function:Disable-LocalUser, function:Get-LocalGroupMember -ErrorAction SilentlyContinue

Write-Host '== §97-D: the created SID is PERSISTED before the privilege verification =='
$spOs = New-D4InMemoryOs -Root $bootRoot
$spSch = New-D4FakeSchtasks -Oses $spOs
$spAcct = New-D4FakeAccountRunner -TaskModel $spSch.State['Tasks'] -QueryAccountFailsAfterCreate $true
$sp = Invoke-D4AccountBootstrap -Contract $contract -RunId $acctRunId -AccountName 'um_share_d4' `
    -ShareTaskName $contract['ShareTaskName'] -DefinitionBuilder $defBuilder -PackageHash $pwPkg `
    -StatePath $bootStatePath -AccountRunner $spAcct -FileBoundary $spOs.Boundary `
    -StoreGuard (New-D4FakeStoreGuard).Guard -StoreProbe $noReparseProbe `
    -AclProbe $script:r97TrustedAclProbe -PlanIdentity $pwPlanIdentity
Assert-True (-not $sp.Ok -and $sp.Refused -eq 'unconfirmed') 'a post-create privilege-query failure is unconfirmed'
$spRec = Read-D4BootstrapState -StatePath $bootStatePath -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwPkg -PlanIdentity $pwPlanIdentity -FileBoundary $spOs.Boundary
Assert-Equal $spRec.status 'unconfirmed' 'the record stays traceable-unconfirmed'
Assert-True ([string]$spRec.created_sid) 'the DISK record retains the SID the creation API returned'
Assert-Equal $spAcct.State['RegisteredCount'] 0 'the unconfirmed creation registers NOTHING'

# ---- R99-A: the structured descriptor decision (GA/D/DC/unknown/missing) ----
Write-Host '== §99-A: foreign GA/D/DC/unknown permissions refuse by RAW BITS =='
$descCases = @(
    @{ tag = 'GenericAll (GA, ACCESS_MASK bit)'; rights = [uint64]268435456 },
    @{ tag = 'Delete (D)'; rights = [System.Security.AccessControl.FileSystemRights]::Delete },
    @{ tag = 'DeleteChild (DC)'; rights = [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles },
    @{ tag = 'ChangePermissions'; rights = [System.Security.AccessControl.FileSystemRights]::ChangePermissions }
)
foreach ($c in $descCases) {
    $cEdge = (New-D4FakeGetAclEdge -DescriptorByPath @{
        $bootRootLow = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
            (New-D4FakeAclRule -IdentitySid $d97ForeignSid -Type 'Allow' -Rights $c.rights)))
    }).Edge
    $cProbe = New-D4RealAclProbe -GetAclEdge $cEdge
    $cRec = & $cProbe $bootRoot @{}
    $cGate = Test-D4AclDescriptorTrusted -Acl $cRec -Path $bootRoot
    Assert-True (-not $cGate.trusted) ('a foreign ' + $c.tag + ' ACE refuses by RAW BITS (§99.2)')
}
# an UNKNOWN permission bit (outside the safe mask, outside the enum) refuses
$uEdge = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $bootRootLow = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        @{ identitySid = $d97ForeignSid; type = 'Allow'; rights = [uint64]4294967296 }))
}).Edge
$uProbe = New-D4RealAclProbe -GetAclEdge $uEdge
$uRec = & $uProbe $bootRoot @{}
$uGate = Test-D4AclDescriptorTrusted -Acl $uRec -Path $bootRoot
Assert-True (-not $uGate.trusted) 'an UNKNOWN permission bit refuses (never read-only by default)'
# a missing ACL (the edge returns no descriptor) refuses
$mProbe = New-D4RealAclProbe -GetAclEdge { param([string]$Path) return $null }
$mRec = & $mProbe $bootRoot @{}
$mGate = Test-D4AclDescriptorTrusted -Acl $mRec -Path $bootRoot
Assert-True (-not $mGate.trusted) 'a missing ACL refuses (cannot prove completeness from zero evidence)'
# the FIRST ACE ordering is preserved: a foreign dangerous rule FIRST refuses
# even when a trusted rule follows (the old text parser dropped the first line)
$fEdge = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $bootRootLow = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        (New-D4FakeAclRule -IdentitySid $d97ForeignSid -Type 'Allow' -Rights ([uint64]268435456)),
        (New-D4FakeAclRule -IdentitySid 'S-1-5-18' -Type 'Allow' -Rights ([System.Security.AccessControl.FileSystemRights]::FullControl))))
}).Edge
$fProbe = New-D4RealAclProbe -GetAclEdge $fEdge
$fRec = & $fProbe $bootRoot @{}
$fGate = Test-D4AclDescriptorTrusted -Acl $fRec -Path $bootRoot
Assert-True (-not $fGate.trusted) 'the FIRST foreign dangerous ACE is judged, never dropped'
# a DENY ACE is conservatively refused
$dEdge = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $bootRootLow = (New-D4FakeSecurityDescriptor -OwnerSid 'S-1-5-18' -Rules @(
        (New-D4FakeAclRule -IdentitySid 'S-1-5-18' -Type 'Allow' -Rights ([System.Security.AccessControl.FileSystemRights]::FullControl)),
        (New-D4FakeAclRule -IdentitySid $d97ForeignSid -Type 'Deny' -Rights ([System.Security.AccessControl.FileSystemRights]::FullControl))))
}).Edge
$dProbe2 = New-D4RealAclProbe -GetAclEdge $dEdge
$dRec2 = & $dProbe2 $bootRoot @{}
$dGate2 = Test-D4AclDescriptorTrusted -Acl $dRec2 -Path $bootRoot
Assert-True (-not $dGate2.trusted) 'a DENY ACE is conservatively refused (§99.2 contract)'

# ---- R99-B: role→GUID fingerprint (a swapped pair is a DIFFERENT plan) ----
Write-Host '== §99-B: the rule fingerprint binds ROLES, not a bare GUID set =='
# the same plan with the permit/block GUIDs SWAPPED -> a different fingerprint
$swappedIds = @($pwRuleIds[1], $pwRuleIds[0], $pwRuleIds[2])
$swappedMap = Get-D4RuleRoleMap -OrderedIds $swappedIds
$swappedFp = Get-D4BootstrapPlanIdentity -RunId $acctRunId -AccountName 'um_share_d4' `
    -TaskName ([string]$contract['ShareTaskName']) -PythonHome 'C:\PythonExp' `
    -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap $swappedMap
$origFp = Get-D4BootstrapPlanIdentity -RunId $acctRunId -AccountName 'um_share_d4' `
    -TaskName ([string]$contract['ShareTaskName']) -PythonHome 'C:\PythonExp' `
    -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap (Get-D4RuleRoleMap -OrderedIds $pwRuleIds)
Assert-True ($swappedFp -ne $origFp) 'swapping the permit/block GUIDs changes the plan fingerprint (roles are bound)'
# merely REORDERING the config rule list (the role mapping unchanged) = the SAME fingerprint
$imagePathPw = $contract['ExperimentRoot'] + '\' + $contract['ShareImage']
$pwRuleEntries = @(
    @{ id = $pwRuleIds[0]; layer = 'connect-v4'; action = 'permit'; app_path = $imagePathPw; remote_addr = '127.0.0.1'; remote_port = 9100 },
    @{ id = $pwRuleIds[1]; layer = 'connect-v4'; action = 'block'; app_path = $imagePathPw },
    @{ id = $pwRuleIds[2]; layer = 'connect-v6'; action = 'block'; app_path = $imagePathPw }
)
    # merely REORDER the LIST (each entry keeps its own id/layer/action -
    # the role mapping is untouched)
    $reorderedRules = @($pwRuleEntries[2], $pwRuleEntries[0], $pwRuleEntries[1])
$reorderedMap = Get-D4RuleRoleMap -Rules $reorderedRules
$reorderedFp = Get-D4BootstrapPlanIdentity -RunId $acctRunId -AccountName 'um_share_d4' `
    -TaskName ([string]$contract['ShareTaskName']) -PythonHome 'C:\PythonExp' `
    -RuntimeSourceDir $rtDir -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap $reorderedMap
Assert-Equal $reorderedFp $origFp 'reordering the config rule list keeps the SAME fingerprint (role mapping unchanged)'
# through the REAL T0: register+config-write-fail, then a SWAPPED-plan re-entry refuses
$swOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$swSch = New-D4FakeSchtasks -Oses $swOs
$swAcct = New-D4FakeAccountRunner -TaskModel $swSch.State['Tasks']
$swPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\plan-sw.json'
Invoke-D4File -Boundary $swOs.Boundary -Operation 'WriteText' -Arguments @($swPlanPath,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $pwRuleIds } | ConvertTo-Json -Depth 5))
$swBootStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
$swEmitCfg = 'C:\ProgramData\UMShareExp-bootstrap\emitted-config-sw.json'
$swSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $swEmitCfg
    SchtasksRunner = $swSch.Runner
    NetStatRunner = (New-D4EmptyRunner -Tag 'net').Runner
    ProcessEvidenceRunner = (New-D4EmptyRunner -Tag 'proc').Runner
    FileBoundary = $swOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = (New-D4HarnessAclRunner -Oses $swOs).Runner
    RulesRunner = (New-D4FakeRuleStore -CreatedFacts @{}).Runner
    AccountRunner = $swAcct
    BootstrapStatePath = $swBootStatePath
    BootstrapPlanPath = $swPlanPath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
$swOrigWrite = $swOs.Boundary['WriteText']
$swOs.Boundary['WriteText'] = {
    param([string]$Path, [string]$Content)
    if ($Path -eq $swEmitCfg) { throw 'simulated config emit failure' }
    & $swOrigWrite $Path $Content
}.GetNewClosure()
Assert-Throws { & $entry -Stage 'T0' @swSplat } 'the first T0 registers but cannot emit' 'simulated config emit failure'
Assert-Equal $swAcct.State['RegisteredCount'] 1 'the first T0 registered exactly once'
# the SWAPPED-plan plan file re-enters the same run
$swPlanSwapped = 'C:\ProgramData\UMShareExp-bootstrap\plan-sw-swapped.json'
Invoke-D4File -Boundary $swOs.Boundary -Operation 'WriteText' -Arguments @($swPlanSwapped,
    (@{ run_id = $acctRunId; account_name = 'um_share_d4'; python_home = 'C:\PythonExp';
        runtime_source_dir = $rtDir; runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha });
        rule_ids = $swappedIds } | ConvertTo-Json -Depth 5))
$swSplat['BootstrapPlanPath'] = $swPlanSwapped
Assert-Throws { & $entry -Stage 'T0' @swSplat } 'a SWAPPED permit/block plan refuses the same-run recovery' 'without this run''s trusted record'
Assert-Equal $swAcct.State['CreateCount'] 1 'the swapped plan costs ZERO additional create calls'
Assert-Equal $swAcct.State['RegisteredCount'] 1 'the swapped plan costs ZERO additional register calls'
Assert-True (-not $swOs.State['Files'].ContainsKey($swEmitCfg.ToLowerInvariant())) 'the swapped plan emits NO config'
# the ORIGINAL plan recovers (IDs and roles both kept)
$swOs.Boundary['WriteText'] = $swOrigWrite
$swSplat['BootstrapPlanPath'] = $swPlanPath
& $entry -Stage 'T0' @swSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'the original plan recovers the same run (IDs and roles kept)'
# collected config with the SWAPPED role GUIDs -> S0 grants NO exemption
# (trusted store, valid record bound to the ORIGINAL plan, registered task
# matching - but the CONFIG's role GUIDs are swapped)
$swcOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$swcSch = New-D4FakeSchtasks -Oses $swcOs
$swcCfgObj = Copy-D4TestConfig @($rulePermit, $ruleBlockV4, $ruleBlockV6)
$swcCfgObj['run_id'] = $acctRunId
$swcCfgObj['share_account'] = 'um_share_d4'
$swcCfgObj['share_sid'] = $shapeSid
$swcCfgObj['logon_method'] = 'password-managed'
$swcCfgObj['runtime_source_dir'] = $rtDir
$swcCfgObj['runtime_files'] = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
$swcCfgObj['limit_rules'][0]['id'] = $pwRuleIds[1]
$swcCfgObj['limit_rules'][1]['id'] = $pwRuleIds[0]
$swcCfgReal = Join-Path $tmpBase 'pw-swapped-config.json'
[IO.File]::WriteAllText($swcCfgReal, ($swcCfgObj | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
$swcRec = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentityComputed
$swcRec.status = 'task-registered'
$swcRec.created_sid = $shapeSid
Invoke-D4File -Boundary $swcOs.Boundary -Operation 'WriteText' -Arguments @($pwBootStatePath, ($swcRec | ConvertTo-Json -Depth 4))
$swcDef = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $shapeSid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$swcSch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $swcDef
$swcSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $swcCfgReal
    SchtasksRunner = $swcSch.Runner
    NetStatRunner = (New-D4EmptyRunner -Tag 'net').Runner
    ProcessEvidenceRunner = (New-D4EmptyRunner -Tag 'proc').Runner
    FileBoundary = $swcOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = (New-D4HarnessAclRunner -Oses $swcOs).Runner
    RulesRunner = (New-D4FakeRuleStore -CreatedFacts @{}).Runner
    AccountRunner = (New-D4FakeAccountRunner -TaskModel $swcSch.State['Tasks'] -SeedSid $shapeSid)
    BootstrapStatePath = $pwBootStatePath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $script:r97TrustedAclProbe
}
Assert-Throws { & $entry -Stage 'S0' @swcSplat } 'a config with SWAPPED role GUIDs grants NO bootstrap exemption' 'S0 inventory verdict=occupied'

# ---- §101: the owner interface is REAL (.NET descriptor, GetOwner) ----
Write-Host '== §101: owner via GetOwner on a REAL in-memory .NET descriptor =='
# §101.3.1: a REAL .NET FileSecurity built ENTIRELY in memory (SYSTEM
# owner + SYSTEM/Admin FullControl) - the GetAclEdge returns it, the REAL
# probe extracts the owner via the REAL GetOwner method, the gate passes
# with the EXACT SYSTEM SID (no fabricated shape can mask an interface
# bug; no real-path Get-Acl, no Set-Acl, no disk)
$r101Sd = New-D4RealInMemoryFileSecurity -OwnerSid 'S-1-5-18' -Rules @(
    @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
    @{ identitySid = 'S-1-5-32-544'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl })
$r101Edge = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $bootRootLow = $r101Sd
}).Edge
$r101Probe = New-D4RealAclProbe -GetAclEdge $r101Edge
$r101Rec = & $r101Probe $bootRoot @{}
Assert-Equal ([string]$r101Rec['ownerSid']) 'S-1-5-18' 'GetOwner([SecurityIdentifier]) returns the EXACT SYSTEM SID from a REAL descriptor'
$r101Gate = Test-D4AclDescriptorTrusted -Acl $r101Rec -Path $bootRoot
Assert-True ([bool]$r101Gate['trusted']) 'a REAL in-memory descriptor with a SYSTEM owner passes the trust gate'
# §101.3.2 negative: a REAL descriptor with a FOREIGN owner refuses
$r101SdF = New-D4RealInMemoryFileSecurity -OwnerSid $d97ForeignSid -Rules @(
    @{ identitySid = $d97ForeignSid; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl })
$r101EdgeF = (New-D4FakeGetAclEdge -DescriptorByPath @{ $bootRootLow = $r101SdF }).Edge
$r101ProbeF = New-D4RealAclProbe -GetAclEdge $r101EdgeF
$r101RecF = & $r101ProbeF $bootRoot @{}
$r101GateF = Test-D4AclDescriptorTrusted -Acl $r101RecF -Path $bootRoot
Assert-True (-not [bool]$r101GateF['trusted']) 'a REAL descriptor with a FOREIGN owner refuses'
# GetOwner-failure stand-in (an exception matching the interface) -> untrusted
$r101SdE = [pscustomobject]@{ dummy = 1 }
$r101SdE | Add-Member -MemberType ScriptMethod -Name GetOwner -Value { throw 'simulated GetOwner failure' }.GetNewClosure()
$r101SdE | Add-Member -MemberType ScriptMethod -Name GetAccessRules -Value { return [object[]]@() }.GetNewClosure()
$r101EdgeE = (New-D4FakeGetAclEdge -DescriptorByPath @{ $bootRootLow = $r101SdE }).Edge
$r101ProbeE = New-D4RealAclProbe -GetAclEdge $r101EdgeE
$r101RecE = & $r101ProbeE $bootRoot @{}
Assert-True ($null -eq $r101RecE['ok']) 'a GetOwner failure is untrusted (never absence)'
# §101.3.3 the consumer chain: S0 passes with a REAL descriptor via the
# real probe (owner through GetOwner)
$r101Os = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$r101Sch = New-D4FakeSchtasks -Oses $r101Os
$r101RecB = New-D4BootstrapState -RunId $acctRunId -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] -PackageHash $pwBootstrapIdentity -PlanIdentity $pwPlanIdentityComputed
$r101RecB.status = 'task-registered'
    # the record/task identity must be the one the PW ENTRY flow actually
    # bound (the emitted config's share_sid = the bootstrap record's SID)
    $r101RecB.created_sid = [string]$pwRec.created_sid
    Invoke-D4File -Boundary $r101Os.Boundary -Operation 'WriteText' -Arguments @($pwBootStatePath, ($r101RecB | ConvertTo-Json -Depth 4))
    $r101Def = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = [string]$pwRec.created_sid } -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$r101Sch.State['Tasks'][$contract['ShareTaskName'].ToLowerInvariant()] = $r101Def
$r101ProbeB = New-D4RealAclProbe -GetAclEdge (New-D4FakeGetAclEdge -AlwaysTrusted).Edge
$r101Splat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $pwCfgReal
    SchtasksRunner = $r101Sch.Runner
    NetStatRunner = (New-D4EmptyRunner -Tag 'net').Runner
    ProcessEvidenceRunner = (New-D4EmptyRunner -Tag 'proc').Runner
    FileBoundary = $r101Os.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = (New-D4HarnessAclRunner -Oses $r101Os).Runner
    RulesRunner = (New-D4FakeRuleStore -CreatedFacts @{}).Runner
    AccountRunner = (New-D4FakeAccountRunner -TaskModel $r101Sch.State['Tasks'] -SeedSid $shapeSid)
    BootstrapStatePath = $pwBootStatePath
    BootstrapStoreGuard = (New-D4FakeStoreGuard).Guard
    BootstrapAclProbe = $r101ProbeB
}
& $entry -Stage 'S0' @r101Splat | Out-Null
Assert-Equal $LASTEXITCODE 0 'the consumer chain passes with a REAL descriptor (owner via GetOwner)'

# ---- §107.2: absence is ENUMERATION evidence (no message keywords) ----
Write-Host '== §107.2: enumeration-based absence via the REAL query ops =='
# cmdlet-boundary shims: Get-LocalUser enumerates the account collection;
# Get-ScheduledTask enumerates the ROOT folder; Export-ScheduledTask
# scriptable per-task. Only the cmdlet boundary is fake - the REAL
# query ops run unchanged (enumeration + exact-name filter + separate
# export verdict)
$script:d107Users = $null
$script:d107EnumThrows = $null
$script:d107Tasks = @{}
$script:d107TaskEnumThrows = $null
$script:d107ExportThrows = @{}
$script:d107NewLocalUserCalls = 0
$script:d107RegisterCalls = 0
function script:Get-LocalUser {
    # THREE real call shapes: ENUMERATION (no params, the §107.2 query
    # op), by-NAME (the create op's read-back) and by-SID (the disable
    # pre-prove) - faithful to the real cmdlet's parameter sets
    param([string]$Name, [string]$SID)
    if ($script:d107EnumThrows) { throw $script:d107EnumThrows }
    $m = @($script:d107Users)
    if ($PSBoundParameters.ContainsKey('SID')) { $m = @($m | Where-Object { $_.SID.Value -ceq $SID }) }
    if ($PSBoundParameters.ContainsKey('Name')) { $m = @($m | Where-Object { $_.Name -ceq $Name }) }
    if ($PSBoundParameters.ContainsKey('SID') -or $PSBoundParameters.ContainsKey('Name')) {
        if ($m.Count -gt 0) { return $m[0] }
        return $null
    }
    return [object[]]$m
}
function script:New-LocalUser {
    # the CREATE boundary: counts every create call and appends the
    # created user to the scripted collection (the real cmdlet writes
    # to the system, making it visible to the follow-up read-back)
    param([string]$Name, [System.Security.SecureString]$Password,
          [switch]$AccountNeverExpires, [switch]$PasswordNeverExpires)
    $script:d107NewLocalUserCalls++
    $u = [pscustomobject]@{ Name = [string]$Name; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-NEW' }; Enabled = $true }
    $script:d107Users = @($script:d107Users + $u)
    return $u
}
function script:Register-ScheduledTask {
    # counts and REFUSES: the query ops must never reach the register
    # boundary (a call here is a structural failure AND would attempt a
    # real registration if unshimmed)
    param([string]$Xml, [string]$TaskName, [string]$TaskPath, [string]$User, [string]$Password)
    $script:d107RegisterCalls++
    throw ('query ops must never register (call #' + [string]$script:d107RegisterCalls + ')')
}
function script:Get-LocalGroupMember {
    param([string]$SID)
    return @()
}
function script:Get-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath)
    if ($script:d107TaskEnumThrows) { throw $script:d107TaskEnumThrows }
    # the REAL queryTask enumerates the ROOT folder (no -TaskName) and
    # filters by exact name itself - the shim returns ALL scripted tasks
    return [object[]]($script:d107Tasks.Values | ForEach-Object { $_ })
}
function script:Export-ScheduledTask {
    # pipeline binding (the real cmdlet consumes pipeline input)
    $Task = @($input)[0]
    $key = [string]$Task.TaskName.ToLowerInvariant()
    if ($script:d107ExportThrows.ContainsKey($key)) { throw $script:d107ExportThrows[$key] }
    return ('<exported>' + $key + '</exported>')
}
$d107Runner = New-D4AccountRunner
# 1. QueryAccount: target truly ABSENT (enumeration completes, zero matches)
$script:d107Users = @([pscustomobject]@{ Name = 'SomeoneElse'; SID = [pscustomobject]@{ Value = 'S-1-5-18' }; Enabled = $true })
$d107Absent = & $d107Runner['QueryAccount'] 'um_share_d4' @{}
Assert-Equal ([string]$d107Absent['present']) 'False' 'a COMPLETED enumeration with zero matches proves ABSENT (§107.2)'
# 2. target EXISTS -> full facts
$script:d107Users = @(
    [pscustomobject]@{ Name = 'SomeoneElse'; SID = [pscustomobject]@{ Value = 'S-1-5-18' }; Enabled = $true },
    [pscustomobject]@{ Name = 'um_share_d4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-9' }; Enabled = $true })
$d107Found = & $d107Runner['QueryAccount'] 'um_share_d4' @{}
Assert-Equal ([string]$d107Found['present']) 'True' 'an enumerated exact-name match returns the full facts'
Assert-Equal ([string]$d107Found['sid']) 'S-1-5-21-777-77-9' 'the found SID is carried'
# 3. enumeration throws an UNKNOWN error containing 未找到 -> present=$null (NEVER absent)
$script:d107EnumThrows = '未找到依赖组件（一个提及目标名的提供程/依赖错误）'
$d107Unk = & $d107Runner['QueryAccount'] 'um_share_d4' @{}
Assert-True ($null -eq $d107Unk['present']) 'an enumeration FAILURE stays unconfirmed even if the message mentions the target (§107.2)'
# 4. QueryTask: target ABSENT (root enumeration completes, zero matches)
$script:d107Tasks = @{ 'someothertask' = [pscustomobject]@{ TaskName = 'SomeOtherTask'; TaskPath = '\'; dummy = 1 } }
$script:d107TaskEnumThrows = $null
$script:d107ExportThrows = @{}
$d107TaskAbsent = & $d107Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-Equal ([string]$d107TaskAbsent['present']) 'False' 'a COMPLETED root-task enumeration with zero matches proves task ABSENT'
# 5. QueryTask: target EXISTS -> full facts (present + exported xml)
$script:d107Tasks['umshare-share-exp'] = [pscustomobject]@{ TaskName = 'umshare-share-exp'; TaskPath = '\' }
$d107TaskFound = & $d107Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-Equal ([string]$d107TaskFound['present']) 'True' 'an enumerated task match returns present + xml'
Assert-True ([string]$d107TaskFound['xml'] -match 'umshare-share-exp') 'the exported definition is carried'
# 6. the task EXISTS but Export throws 含未找到 -> present=$null (found-but-unverifiable, NEVER absent)
$script:d107ExportThrows['umshare-share-exp'] = '导出失败：未找到某依赖组件'
$d107TaskExp = & $d107Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-True ($null -eq $d107TaskExp['present']) 'an EXPORT failure for an EXISTING task is unconfirmed, NEVER absent (§107.2)'
Assert-True ([string]$d107TaskExp['error'] -match 'EXISTING task') 'the refusal names found-but-unverifiable'
# 7. the TASK ENUMERATION throws an unknown error -> present=$null
$script:d107TaskEnumThrows = '枚举失败：未找到调度程序提供程'
$d107TaskUnk = & $d107Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-True ($null -eq $d107TaskUnk['present']) 'a task-enumeration failure stays unconfirmed (never absent)'
# 9. §109.2: a CASE-VARIANT account is a collision, NEVER absent
# (Windows account names are case-insensitive; OrdinalIgnoreCase
# full equality - the facts flow on to the SID ownership check)
$script:d107EnumThrows = $null
$script:d107Users = @([pscustomobject]@{ Name = 'UM_SHARE_D4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-9' }; Enabled = $true })
$d107Var = & $d107Runner['QueryAccount'] 'um_share_d4' @{}
Assert-Equal ([string]$d107Var['present']) 'True' 'a case-variant account match is PRESENT, never absent (§109.2)'
Assert-Equal ([string]$d107Var['sid']) 'S-1-5-21-777-77-9' 'the case-variant account facts are carried for the SID ownership check (§109.2)'
# 10. a CASE-VARIANT root task is an EXISTING candidate, NEVER absent
$script:d107TaskEnumThrows = $null
$script:d107ExportThrows = @{}
$script:d107Tasks = @{ 'other' = [pscustomobject]@{ TaskName = 'SomeOtherTask'; TaskPath = '\' } }
$script:d107Tasks['UMShare-Share-Exp'] = [pscustomobject]@{ TaskName = 'UMShare-Share-Exp'; TaskPath = '\' }
$d107TaskVar = & $d107Runner['QueryTask'] 'umshare-share-exp' @{}
Assert-Equal ([string]$d107TaskVar['present']) 'True' 'a case-variant task match is an EXISTING candidate, never absent (§109.2)'
# 8. the failure branches above trigger ZERO create/register calls - the
# counters track the REAL cmdlet boundary (all query cases including
# the §109.2 collisions), and the positive control below proves the
# counters are live (not trivially zero)
Assert-Equal $script:d107NewLocalUserCalls 0 'all query/failure cases above triggered ZERO create calls (§107.2/§109.2)'
Assert-Equal $script:d107RegisterCalls 0 'the query ops never reached the register boundary (§107.2/§109.2)'
# positive control: the create boundary IS tracked through the same runner
$script:d107EnumThrows = $null
$script:d107Users = @([pscustomobject]@{ Name = 'SomeoneElse'; SID = [pscustomobject]@{ Value = 'S-1-5-18' }; Enabled = $true })
$d107Create = & $d107Runner['CreateAccount'] 'um_share_d4' ([System.Security.SecureString]::new()) @{}
Assert-Equal $script:d107NewLocalUserCalls 1 'the tracked create boundary counts a REAL create call (positive control)'
Assert-Equal ([string]$d107Create['sid']) 'S-1-5-21-777-77-NEW' 'the create read-back returns the created SID through the same boundary'
Remove-Item function:Get-LocalUser, function:New-LocalUser, function:Get-LocalGroupMember, function:Get-ScheduledTask, function:Export-ScheduledTask, function:Register-ScheduledTask -ErrorAction SilentlyContinue


# ---- §115: authorized same-SID recovery (Invoke-D4AccountRecovery) ----
Write-Host '== §115: recovery entry - fixed SID, reset/enable/register each EXACTLY once =='
$d115Fixed = 'S-1-5-21-777-77-1007'
$d115OldRun = 'd4x-20260911-01'
$d115NewRun = 'd4x-20260911-02'
$d115Root = 'c:\programdata\umshareexp-bootstrap'
$d115OldPath = Join-Path $d115Root ('bootstrap-state-' + $d115OldRun + '.json')
$d115RecPath = Join-Path $d115Root ('bootstrap-recovery-' + $d115NewRun + '.json')
$d115Pkg = 'pkg-115'
$d115Plan = 'plan-115'
$d115Contract = @{
    BootstrapRoot    = $d115Root
    RecoveryStateRel = 'bootstrap-recovery-<runid>.json'
    ExperimentRoot   = $contract['ExperimentRoot']
    BrokerTaskName   = $contract['BrokerTaskName']
    ShareTaskName    = $contract['ShareTaskName']
}
function New-D4OldRunJson {
    param([string]$RunId, [string]$Sid)
    return (@{
        schema = 'd4-account-bootstrap'; run_id = $RunId; account_name = 'um_share_d4'
        task_name = $contract['ShareTaskName']; package_hash = 'old-pkg'; plan_identity = 'old-plan'
        status = 'unconfirmed'; created_sid = $Sid
        intent_at = ''; created_at = 't0'; task_registered_at = ''
        unconfirmed_reason = 'historical'; task_register_result = 'failed'
    } | ConvertTo-Json -Depth 4)
}
function New-D4FakeRecoveryRunner {
    # §115 fixture: an account model stopped JUST AFTER creation - present,
    # DISABLED, non-admin; the recovery ops count calls and record the SID the
    # reset/enable were addressed BY (password VALUE is never stored - only
    # its length as a flow token).
    param([hashtable]$TaskModel, [string]$FixedSid,
          [bool]$AccountAdmin = $false,
          [bool]$AccountEnabled = $false,
          [string]$LiveSidOverride = '',
          [switch]$ResetFails, [switch]$EnableFails, [switch]$RegisterFails,
          [switch]$DisableFails, [switch]$TaskQueryUnconfirmed)
    $st = @{
        FixedSid = $FixedSid
        IsAdmin = $AccountAdmin
        Enabled = $AccountEnabled
        LiveSid = $(if ($LiveSidOverride) { $LiveSidOverride } else { $FixedSid })
        ResetFails = [bool]$ResetFails
        EnableFails = [bool]$EnableFails
        RegisterFails = [bool]$RegisterFails
        DisableFails = [bool]$DisableFails
        TaskQueryUnconfirmed = [bool]$TaskQueryUnconfirmed
        DisableCount = 0
        ResetCount = 0; EnableCount = 0; RegisterCount = 0
        ResetSids = New-Object System.Collections.Generic.List[string]
        EnableSids = New-Object System.Collections.Generic.List[string]
        RegisterUsers = New-Object System.Collections.Generic.List[string]
        PasswordLen = 0
        PlainPwLen = 0
    }
    return @{
        State = $st
        QueryAccount = {
            param([string]$Name, [hashtable]$Ctx)
            return @{ present = $true; sid = [string]$st['LiveSid']; enabled = $st['Enabled']
                      is_admin = $st['IsAdmin']; error = $null }
        }.GetNewClosure()
        QueryAccountBySid = {
            param([string]$Sid, [hashtable]$Ctx)
            if ([string]::Equals($Sid, [string]$st['FixedSid'], [System.StringComparison]::OrdinalIgnoreCase)) {
                return @{ present = $true; sid = [string]$st['FixedSid']; enabled = $st['Enabled']
                          is_admin = $st['IsAdmin']; error = $null }
            }
            return @{ present = $false; sid = $null; enabled = $null; is_admin = $null; error = $null }
        }.GetNewClosure()
        ResetAccountPasswordBySid = {
            param([string]$Sid, [System.Security.SecureString]$Password, [hashtable]$Ctx)
            $st['ResetCount']++
            $st['ResetSids'].Add([string]$Sid)
            $st['PasswordLen'] = $Password.Length
            $st['ResetCaptured'] = $Password   # §147: value capture for the equality check (never output)
            if ($st['ResetFails']) { return @{ ok = $false; error = 'simulated reset failure' } }
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
        EnableAccountBySid = {
            param([string]$Sid, [hashtable]$Ctx)
            $st['EnableCount']++
            $st['EnableSids'].Add([string]$Sid)
            if ($st['EnableFails']) { return @{ ok = $false; error = 'simulated enable failure' } }
            $st['Enabled'] = $true
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
        RegisterShareTask = {
            param([string]$TaskName, [string]$TaskPath, [string]$DefinitionXml,
                  [string]$UserName, [string]$PlainPassword, [hashtable]$Ctx)
            if ([string]$TaskPath -ne '\') { return @{ ok = $false; error = 'TaskPath must be the root folder' } }
            if ($st['RegisterFails']) { return @{ ok = $false; error = 'simulated registration failure' } }
            if ($TaskModel.ContainsKey($TaskName.ToLowerInvariant())) { return @{ ok = $false; error = 'task already exists' } }
            $st['RegisterCount']++
            $st['RegisterUsers'].Add([string]$UserName)
            $st['PlainPwLen'] = $PlainPassword.Length
            $st['RegisterCaptured'] = $PlainPassword   # §147: value capture for the equality check (never output)
            $TaskModel[$TaskName.ToLowerInvariant()] = $DefinitionXml
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
        QueryTask = {
            param([string]$TaskName, [hashtable]$Ctx)
            if ($st['TaskQueryUnconfirmed']) {
                return @{ present = $null; xml = $null; error = 'simulated enumeration failure' }
            }
            if ($TaskModel.ContainsKey($TaskName.ToLowerInvariant())) {
                return @{ present = $true; xml = $TaskModel[$TaskName.ToLowerInvariant()]; error = $null }
            }
            return @{ present = $false; xml = $null; error = $null }
        }.GetNewClosure()
        DisableAccount = {
            param([string]$Sid, [hashtable]$Ctx)
            $st['DisableCount']++
            if ($st['DisableFails']) { return @{ ok = $false; error = 'simulated disable failure' } }
            $st['Enabled'] = $false
            return @{ ok = $true; error = $null }
        }.GetNewClosure()
    }
}
function New-D115Env {
    # Fresh in-memory store + old record + task model + runner + occupancy;
    # -UseOs reuses an existing store (idempotent re-entry keeps its records);
    # -ExpectedShaOverride forces a mismatching frozen baseline (§116.2);
    # -WrapBoundary injects a write-failure wrapper (simulated accounting
    # write failures that must still run the bounded close, §116.4).
    param([string]$OldJson, [hashtable]$TaskModel, [hashtable]$Runner,
          [scriptblock]$OccProbe, [scriptblock]$AclEdge, [hashtable]$UseOs = $null,
          [string]$ExpectedShaOverride = '', [scriptblock]$WrapBoundary = $null)
    $envOs = $(if ($UseOs) { $UseOs } else { New-D4InMemoryOs -Root $d115Root })
    if ($WrapBoundary) {
        $envOs['Boundary'] = (& $WrapBoundary $envOs.Boundary)
    }
    if ($OldJson) {
        Invoke-D4File -Boundary $envOs.Boundary -Operation 'WriteText' -Arguments @($d115OldPath, $OldJson)
    }
    $envDef = {
        param([string]$Sid)
        New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $Sid } `
            -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
    }.GetNewClosure()
    $envAcl = New-D4RealAclProbe -GetAclEdge $AclEdge
    $envProbe = { param([string]$Path) return $null }
    # §116.2: the frozen expected old hash is the WRITTEN old record's hash
    # (the reviewer-supplied external baseline the module must match).
    $envExpectedSha = ''
    if ($OldJson) {
        $envExpectedSha = [string](Invoke-D4File -Boundary $envOs.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
    }
    if ($ExpectedShaOverride) { $envExpectedSha = $ExpectedShaOverride }
    $recRes = Invoke-D4AccountRecovery -Contract $d115Contract -RunId $d115NewRun `
        -AccountName 'um_share_d4' -ShareTaskName $contract['ShareTaskName'] `
        -OldRunId $d115OldRun -OldStatePath $d115OldPath -FixedSid $d115Fixed `
        -ExpectedOldStateSha256 $envExpectedSha `
        -DefinitionBuilder $envDef -PackageHash $d115Pkg -PlanIdentity $d115Plan `
        -RecoveryStatePath $d115RecPath -AccountRunner $Runner `
        -FileBoundary $envOs.Boundary -StoreProbe $envProbe -AclProbe $envAcl `
        -OccupancyProbe $OccProbe
    return @{ Os = $envOs; Result = $recRes; ExpectedOldSha = $envExpectedSha }
}
$d115AclTrusted = (New-D4FakeGetAclEdge -AlwaysTrusted).Edge
$d115AclFail = (New-D4FakeGetAclEdge -Fails).Edge
$d115OccClean = { return @{ clean = $true; detail = '' } }
$d115OccBusy = { return @{ clean = $false; detail = 'simulated experiment task present' } }
# C1: correct same-SID recovery - reset/enable/register each EXACTLY once,
#     task-registered record, old record bytes unchanged, no credential inside.
$t115 = @{}
$run115 = New-D4FakeRecoveryRunner -TaskModel $t115 -FixedSid $d115Fixed
$oldJson115 = New-D4OldRunJson -RunId $d115OldRun -Sid $d115Fixed
$e115 = New-D115Env -OldJson $oldJson115 -TaskModel $t115 -Runner $run115 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True ([bool]$e115.Result.Ok) ('a correct same-SID recovery completes (result=' + ($e115.Result | ConvertTo-Json -Compress) + ')')
Assert-Equal ([string]$e115.Result.Status) 'task-registered' 'the recovery returns task-registered'
Assert-Equal ([string]$e115.Result.CreatedSid) $d115Fixed 'the recovery returns the fixed SID'
Assert-Equal $run115.State.ResetCount 1 'the password reset is performed EXACTLY once'
Assert-Equal $run115.State.EnableCount 1 'the enable is performed EXACTLY once'
Assert-Equal $run115.State.RegisterCount 1 'the registration is performed EXACTLY once'
Assert-Equal ([string]$run115.State.ResetSids[0]) $d115Fixed 'the reset is addressed BY the fixed SID'
Assert-Equal ([string]$run115.State.EnableSids[0]) $d115Fixed 'the enable is addressed BY the fixed SID'
Assert-True ($run115.State.PasswordLen -ge 12) 'a real strong password was generated (length token)'
Assert-True ($t115.ContainsKey([string]$contract['ShareTaskName'].ToLowerInvariant())) 'the task model received the registered definition'
$recJson115 = [string](Invoke-D4File -Boundary $e115.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJson115 -notmatch 'password') 'the recovery record carries NO credential field'
Assert-True ($recJson115 -match '"status"\s*:\s*"task-registered"') 'the recovery record is task-registered'
$oldReadBack = [string](Invoke-D4File -Boundary $e115.Os.Boundary -Operation 'ReadText' -Arguments @($d115OldPath))

# §147 credential-chain: the SAME in-process secret reaches BOTH the reset
# and the registration boundaries, and the registration account resolves to
# the SAME SID the reset addressed (equality/length/type booleans only - the
# secret value is never written, printed or hashed).
$t147 = @{}
$run147 = New-D4FakeRecoveryRunner -TaskModel $t147 -FixedSid $d115Fixed
$old147 = New-D4OldRunJson -RunId $d115OldRun -Sid $d115Fixed
$e147 = New-D115Env -OldJson $old147 -TaskModel $t147 -Runner $run147 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True ([bool]$e147.Result.Ok) '§147 a control recovery completes through the captured edges'
$chResetSecure = $run147.State.ResetCaptured
$chRegisterPlain = [string]$run147.State.RegisterCaptured
$chResetPlain = ''
if ($null -ne $chResetSecure) { $chResetPlain = (New-Object System.Net.NetworkCredential('', $chResetSecure)).Password }
$chEq = ($chResetPlain.Length -gt 0) -and ($chRegisterPlain.Length -gt 0) -and
        ([string]::Equals($chResetPlain, $chRegisterPlain, [System.StringComparison]::Ordinal))
Assert-True $chEq '§147 the RESET and REGISTER boundaries received the SAME password VALUE (equality boolean only)'

# R147 regression: the PARENTHESIZED extraction returns the EXACT test secret -
# the unparenthesized `New-Object Type(a, $b).Password` form returns the OBJECT
# (its [string] is the literal type name 'System.Net.NetworkCredential', which
# was what the live registration received, causing 4625 SubStatus 0xC000006A).
$knownSecret = 'TestSecretAbc123!'
$knownSS = ConvertTo-SecureString -String $knownSecret -AsPlainText -Force
$extracted = (New-Object System.Net.NetworkCredential('', $knownSS)).Password
Assert-True ($extracted -ceq $knownSecret) '§147 the parenthesized credential extraction returns the EXACT test secret (value equality boolean only)'
$unparenResult = New-Object System.Net.NetworkCredential('', $knownSS).Password
Assert-True ($unparenResult -is [System.Net.NetworkCredential]) '§147 the UNparenthesized extraction yields the OBJECT (the historical corruption shape)'
Assert-True ([string]$unparenResult -eq 'System.Net.NetworkCredential') '§147 the corrupt object stringifies to the literal type name (the registration-time wrong password)'

Assert-True (($chResetPlain.Length -eq $chRegisterPlain.Length) -and ($chResetPlain.Length -ge 16)) '§147 the shared secret LENGTH token is consistent (>=16)'
Assert-True (($null -ne $chResetSecure) -and ($chResetSecure -is [System.Security.SecureString]) -and ($chRegisterPlain -is [string])) '§147 the TYPES are SecureString in at reset, plain String at register'
Assert-Equal ([string]$run147.State.ResetSids[0]) $d115Fixed '§147 the reset is addressed by the fixed SID'
Assert-Equal ([string]$run147.State.RegisterUsers[0]) 'um_share_d4' '§147 the registration uses the fixed account NAME'
Assert-Equal ([string]$e147.Result.CreatedSid) $d115Fixed '§147 the recovery binds the fixed SID (name resolves to the same SID as the reset)'
Assert-True ($oldReadBack.Trim() -eq $oldJson115.Trim()) 'the OLD creation record bytes are untouched by recovery'
Assert-Equal ((Invoke-D4File -Boundary $e115.Os.Boundary -Operation 'Hash' -Arguments @($d115RecPath)) `
    -ne $null) $true 'the recovery record is persisted in the protected store'
$d115OldHashAtComplete = [string](Invoke-D4File -Boundary $e115.Os.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
# C2: re-entry after COMPLETION is idempotent - no second reset/enable/register.
$e115b = New-D115Env -OldJson $oldJson115 -TaskModel $t115 -Runner $run115 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted -UseOs $e115.Os
Assert-True ([bool]$e115b.Result.Ok) ('a completed recovery re-entry stays Ok (idempotent): ' + ($e115b.Result | ConvertTo-Json -Compress))
Assert-Equal $run115.State.ResetCount 1 'the idempotent re-entry performs ZERO additional resets'
Assert-Equal $run115.State.EnableCount 1 'the idempotent re-entry performs ZERO additional enables'
Assert-Equal $run115.State.RegisterCount 1 'the idempotent re-entry performs ZERO additional registrations'
# C3: WRONG SID (old record's created_sid differs from the fixed recovery SID) -> zero actions, no record.
$t115c = @{}
$run115c = New-D4FakeRecoveryRunner -TaskModel $t115c -FixedSid $d115Fixed
$e115c = New-D115Env -OldJson (New-D4OldRunJson -RunId $d115OldRun -Sid 'S-1-5-21-777-77-9999') `
    -TaskModel $t115c -Runner $run115c -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115c.Result.Ok) 'a WRONG recorded SID refuses the recovery'
Assert-Equal ([string]$e115c.Result.Refused) 'eligibility' 'the wrong-SID refusal names eligibility'
Assert-Equal $run115c.State.ResetCount 0 'the wrong-SID refusal performs ZERO resets'
Assert-Equal $run115c.State.EnableCount 0 'the wrong-SID refusal performs ZERO enables'
Assert-Equal $run115c.State.RegisterCount 0 'the wrong-SID refusal performs ZERO registrations'
Assert-True (-not (Invoke-D4File -Boundary $e115c.Os.Boundary -Operation 'IsLeaf' -Arguments @($d115RecPath))) 'the wrong-SID refusal writes NO recovery record'
# C4: SAME-NAME account carrying a DIFFERENT SID -> zero actions.
$t115d = @{}
$run115d = New-D4FakeRecoveryRunner -TaskModel $t115d -FixedSid $d115Fixed -LiveSidOverride 'S-1-5-21-777-77-2007'
$e115d = New-D115Env -OldJson $oldJson115 -TaskModel $t115d -Runner $run115d `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115d.Result.Ok) 'a same-name account with a DIFFERENT live SID refuses'
Assert-Equal $run115d.State.ResetCount 0 'the different-live-SID refusal performs ZERO resets'
Assert-Equal $run115d.State.EnableCount 0 'the different-live-SID refusal performs ZERO enables'
# C5: an ADMIN account -> zero actions.
$t115e = @{}
$run115e = New-D4FakeRecoveryRunner -TaskModel $t115e -FixedSid $d115Fixed -AccountAdmin $true
$e115e = New-D115Env -OldJson $oldJson115 -TaskModel $t115e -Runner $run115e `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115e.Result.Ok) 'an admin account refuses the recovery'
Assert-Equal $run115e.State.ResetCount 0 'the admin refusal performs ZERO resets'
Assert-Equal $run115e.State.EnableCount 0 'the admin refusal performs ZERO enables'
# C6: an ENABLED account (recovery requires currently disabled) -> zero actions.
$t115f = @{}
$run115f = New-D4FakeRecoveryRunner -TaskModel $t115f -FixedSid $d115Fixed -AccountEnabled $true
$e115f = New-D115Env -OldJson $oldJson115 -TaskModel $t115f -Runner $run115f `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115f.Result.Ok) 'an already-ENABLED account refuses (recovery requires currently disabled)'
Assert-Equal $run115f.State.ResetCount 0 'the enabled-account refusal performs ZERO resets'
# C7: an UNTRUSTED store -> refuse before anything, zero actions.
$t115g = @{}
$run115g = New-D4FakeRecoveryRunner -TaskModel $t115g -FixedSid $d115Fixed
$e115g = New-D115Env -OldJson $oldJson115 -TaskModel $t115g -Runner $run115g `
    -OccProbe $d115OccClean -AclEdge $d115AclFail
Assert-True (-not $e115g.Result.Ok) 'an untrusted store refuses the recovery'
Assert-Equal ([string]$e115g.Result.Refused) 'store-not-protected' 'the untrusted store names store-not-protected'
Assert-Equal $run115g.State.ResetCount 0 'the untrusted-store refusal performs ZERO resets'
# C8: experiment occupancy -> zero actions.
$t115h = @{}
$run115h = New-D4FakeRecoveryRunner -TaskModel $t115h -FixedSid $d115Fixed
$e115h = New-D115Env -OldJson $oldJson115 -TaskModel $t115h -Runner $run115h `
    -OccProbe $d115OccBusy -AclEdge $d115AclTrusted
Assert-True (-not $e115h.Result.Ok) 'experiment occupancy refuses the recovery'
Assert-Equal ([string]$e115h.Result.Refused) 'eligibility' 'the occupancy refusal names eligibility'
Assert-Equal $run115h.State.ResetCount 0 'the occupancy refusal performs ZERO resets'
# C9: task registration FAILURE after reset+enable -> unconfirmed WITH the
#     fixed SID (so the SID-addressed disable stays executable).
$t115i = @{}
$run115i = New-D4FakeRecoveryRunner -TaskModel $t115i -FixedSid $d115Fixed -RegisterFails
$e115i = New-D115Env -OldJson $oldJson115 -TaskModel $t115i -Runner $run115i `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115i.Result.Ok) ('a registration failure refuses the recovery: ' + ($e115i.Result | ConvertTo-Json -Compress))
Assert-Equal ([string]$e115i.Result.Status) 'unconfirmed' 'the registration failure lands unconfirmed'
Assert-Equal ([string]$e115i.Result.CreatedSid) $d115Fixed 'the unconfirmed result still carries the fixed SID for the disable step'
Assert-Equal $run115i.State.ResetCount 1 'the registration-failure path performed exactly one reset'
Assert-Equal $run115i.State.EnableCount 1 'the registration-failure path performed exactly one enable'
# §116.4: the register-failure CLOSE is part of the real orchestration -
# DisableAccount is called once and the SAME SID read back Enabled=False,
# then the record is marked disabled preserving the original reason.
Assert-Equal $run115i.State.DisableCount 1 'the registration failure triggers the bounded disable EXACTLY once'
Assert-True (-not $run115i.State.Enabled) 'the bounded close reads the same SID back as disabled'
$recJsonI = [string](Invoke-D4File -Boundary $e115i.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonI -match '"status"\s*:\s*"disabled"') 'the closed record is persisted as disabled'
Assert-True ($recJsonI -match 'task registration failed') 'the original failure reason is preserved in the closed record'
# C10: re-entry on an INTERRUPTED record (recovery-intent persisted) -> refused,
#      ZERO second reset (the password may already be lost in-process).
# (a fresh runner is used; the interrupted RECORD is seeded directly below.)
$t115j = @{}
$run115j = New-D4FakeRecoveryRunner -TaskModel $t115j -FixedSid $d115Fixed
$e115j2 = New-D4InMemoryOs -Root $d115Root
Invoke-D4File -Boundary $e115j2.Boundary -Operation 'WriteText' -Arguments @($d115OldPath, $oldJson115)
$j2OldSha = [string](Invoke-D4File -Boundary $e115j2.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
$intRec = (@{
    schema = 'd4-account-recovery'; run_id = $d115NewRun; account_name = 'um_share_d4'
    task_name = $contract['ShareTaskName']; created_state_path = $d115OldPath
    created_state_sha256 = $j2OldSha; created_sid = $d115Fixed; package_hash = $d115Pkg
    plan_identity = $d115Plan; status = 'recovery-intent'
    intent_at = 't-intent'; reset_at = ''; enabled_at = ''; task_registered_at = ''
    disabled_at = ''; unconfirmed_reason = ''; task_register_result = ''
} | ConvertTo-Json -Depth 4)
Invoke-D4File -Boundary $e115j2.Boundary -Operation 'WriteText' -Arguments @($d115RecPath, $intRec)
$envDef2 = {
    param([string]$Sid)
    New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $Sid } `
        -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
}.GetNewClosure()
$j2Expected = [string](Invoke-D4File -Boundary $e115j2.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
$resJ2 = Invoke-D4AccountRecovery -Contract $d115Contract -RunId $d115NewRun `
    -AccountName 'um_share_d4' -ShareTaskName $contract['ShareTaskName'] `
    -OldRunId $d115OldRun -OldStatePath $d115OldPath -FixedSid $d115Fixed `
    -ExpectedOldStateSha256 $j2Expected `
    -DefinitionBuilder $envDef2 -PackageHash $d115Pkg -PlanIdentity $d115Plan `
    -RecoveryStatePath $d115RecPath -AccountRunner $run115j `
    -FileBoundary $e115j2.Boundary -StoreProbe { param([string]$Path) return $null } `
    -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted) -OccupancyProbe $d115OccClean
Assert-True (-not $resJ2.Ok) 'an interrupted (recovery-intent) record refuses any second attempt'
# §118.2: the interrupted re-entry ALSO runs ONE bounded close (no reset/enable).
Assert-Equal ([string]$resJ2.Refused) 'recovery-attempted-closed' 'the interrupted-record refusal names recovery-attempted-closed'
Assert-Equal $run115j.State.ResetCount 0 'an interrupted record triggers ZERO second resets'
Assert-Equal $run115j.State.EnableCount 0 'an interrupted record triggers ZERO second enables'
Assert-Equal $run115j.State.RegisterCount 0 'an interrupted record triggers ZERO registrations'
Assert-Equal $run115j.State.DisableCount 1 'the interrupted re-entry performs ONE bounded disable'
# C11: the recovery record PLAN DRIFT refuses (binding mismatch -> unreadable).
# The drifted record is seeded as COMPLETED (task-registered) with the task
# actually present - WITHOUT the plan-binding read, the idempotent path would
# return Ok, so this case truly discriminates the binding check.
$e115k = New-D4InMemoryOs -Root $d115Root
Invoke-D4File -Boundary $e115k.Boundary -Operation 'WriteText' -Arguments @($d115OldPath, $oldJson115)
$doneObj = ($intRec | ConvertFrom-Json)
$doneObj.status = 'task-registered'
$doneObj.task_registered_at = 't-done'
$doneObj.plan_identity = 'plan-999'
$driftRec = ($doneObj | ConvertTo-Json -Depth 4)
Invoke-D4File -Boundary $e115k.Boundary -Operation 'WriteText' -Arguments @($d115RecPath, $driftRec)
$t115k = @{}
$kDef = New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $d115Fixed } `
    -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$t115k[$contract['ShareTaskName'].ToLowerInvariant()] = $kDef
$run115k = New-D4FakeRecoveryRunner -TaskModel $t115k -FixedSid $d115Fixed
$kExpected = [string](Invoke-D4File -Boundary $e115k.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
$resK = Invoke-D4AccountRecovery -Contract $d115Contract -RunId $d115NewRun `
    -AccountName 'um_share_d4' -ShareTaskName $contract['ShareTaskName'] `
    -OldRunId $d115OldRun -OldStatePath $d115OldPath -FixedSid $d115Fixed `
    -ExpectedOldStateSha256 $kExpected `
    -DefinitionBuilder { param([string]$Sid) New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $Sid } -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password' } `
    -PackageHash $d115Pkg -PlanIdentity $d115Plan `
    -RecoveryStatePath $d115RecPath -AccountRunner $run115k `
    -FileBoundary $e115k.Boundary -StoreProbe { param([string]$Path) return $null } `
    -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted) -OccupancyProbe $d115OccClean
Assert-True (-not $resK.Ok) 'a plan-drifted recovery record refuses (binding mismatch)'
Assert-Equal ([string]$resK.Refused) 'recovery-record-unreadable' 'the plan-drift refusal names recovery-record-unreadable'
Assert-Equal $run115k.State.ResetCount 0 'the plan-drift refusal performs ZERO resets'
Assert-Equal $run115k.State.RegisterCount 0 'the plan-drift refusal performs ZERO registrations'
# C12 slices: a COMPLETED recovery re-entry does NOT consult the occupancy
# probe (the entry's real probe would report THIS run's own share task as
# present) - the fresh-attempt probe stays mandatory.
$t115m = @{}
$run115m = New-D4FakeRecoveryRunner -TaskModel $t115m -FixedSid $d115Fixed
$e115m = New-D115Env -OldJson $oldJson115 -TaskModel $t115m -Runner $run115m `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True ([bool]$e115m.Result.Ok) 'C14 pre-step: a fresh recovery completes against clean occupancy'
$e115m2 = New-D115Env -OldJson $oldJson115 -TaskModel $t115m -Runner $run115m `
    -OccProbe $d115OccBusy -AclEdge $d115AclTrusted -UseOs $e115m.Os
Assert-True ([bool]$e115m2.Result.Ok) ('a COMPLETED recovery re-entry ignores the occupancy probe (own task is expected): ' + ($e115m2.Result | ConvertTo-Json -Compress))
Assert-Equal $run115m.State.ResetCount 1 'the occupancy-skipping re-entry performs ZERO additional resets'
Assert-Equal $run115m.State.RegisterCount 1 'the occupancy-skipping re-entry performs ZERO additional registrations'
# C15: §116.2 - the OLD creation record leaf must pass the SAME read-only
# trust gate: a foreign write ACE on the old leaf refuses with ZERO actions.
$rootDescr115 = New-D4RealInMemoryFileSecurity -OwnerSid 'S-1-5-18' -Rules @(
    @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
    @{ identitySid = 'S-1-5-32-544'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl })
$badOldLeaf115 = New-D4RealInMemoryFileSecurity -OwnerSid 'S-1-5-18' -Rules @(
    @{ identitySid = 'S-1-5-18'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
    @{ identitySid = 'S-1-5-32-545'; type = 'Allow'; rights = [System.Security.AccessControl.FileSystemRights]::Modify })
$edgeMixed115 = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $d115Root.ToLowerInvariant() = $rootDescr115
    $d115OldPath.ToLowerInvariant() = $badOldLeaf115
    $d115RecPath.ToLowerInvariant() = $rootDescr115
}).Edge
$t115x = @{}
$run115x = New-D4FakeRecoveryRunner -TaskModel $t115x -FixedSid $d115Fixed
$e115x = New-D115Env -OldJson $oldJson115 -TaskModel $t115x -Runner $run115x `
    -OccProbe $d115OccClean -AclEdge $edgeMixed115
Assert-True (-not $e115x.Result.Ok) 'an OLD-leaf with a foreign write ACE refuses the recovery'
Assert-Equal $run115x.State.ResetCount 0 'the old-leaf-untrusted refusal performs ZERO resets'
Assert-Equal $run115x.State.EnableCount 0 'the old-leaf-untrusted refusal performs ZERO enables'
Assert-Equal $run115x.State.RegisterCount 0 'the old-leaf-untrusted refusal performs ZERO registrations'
Assert-True (-not (Invoke-D4File -Boundary $e115x.Os.Boundary -Operation 'IsLeaf' -Arguments @($d115RecPath))) 'the old-leaf-untrusted refusal writes NO recovery record'
# C16: §116.2 - a FROZEN expected old hash that does not match the live old
# record refuses with zero actions (never the same read as baseline).
$t115y = @{}
$run115y = New-D4FakeRecoveryRunner -TaskModel $t115y -FixedSid $d115Fixed
$e115y = New-D115Env -OldJson $oldJson115 -TaskModel $t115y -Runner $run115y `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted -ExpectedShaOverride ('0' * 64)
Assert-True (-not $e115y.Result.Ok) 'a frozen expected old hash that does not match the live record refuses'
Assert-Equal ([string]$e115y.Result.Refused) 'eligibility' 'the hash-mismatch refusal names eligibility'
Assert-Equal $run115y.State.ResetCount 0 'the hash-mismatch refusal performs ZERO resets'
Assert-Equal $run115y.State.EnableCount 0 'the hash-mismatch refusal performs ZERO enables'
# C17: §116.4 - a DISABLE failure inside the close NEVER records 'disabled'
# (the record stays in its prior state; the original reason is preserved).
$t115z = @{}
$run115z = New-D4FakeRecoveryRunner -TaskModel $t115z -FixedSid $d115Fixed -RegisterFails -DisableFails
$e115z = New-D115Env -OldJson $oldJson115 -TaskModel $t115z -Runner $run115z `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted
Assert-True (-not $e115z.Result.Ok) 'a register failure with a failing disable still refuses'
Assert-Equal $run115z.State.DisableCount 1 'the bounded disable was ATTEMPTED once'
$recJsonZ = [string](Invoke-D4File -Boundary $e115z.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonZ -notmatch '"status"\s*:\s*"disabled"') 'a failed disable is NEVER recorded as disabled'
Assert-True ($recJsonZ -match '"status"\s*:\s*"unconfirmed"') 'the close failure lands the record unconfirmed (not disabled)'
Assert-True ($recJsonZ -match 'task registration failed') 'the original failure reason is preserved'
Assert-True ([string]$e115z.Result.Reason -match 'recovery close FAILED') 'the returned reason names the close failure'
# C18: §116.4 - the SUCCESS-END closing reuses the same close function: a
# completed (task-registered) recovery record is disabled by SID and the
# disabled terminal is persisted with post-verification.
$e115End = Invoke-D4RecoveryClose -Contract $d115Contract -RunId $d115NewRun `
    -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] `
    -FixedSid $d115Fixed -RecoveryStatePath $d115RecPath `
    -PackageHash $d115Pkg -PlanIdentity $d115Plan `
    -AccountRunner $run115 -FileBoundary $e115.Os.Boundary `
    -StoreProbe { param([string]$Path) return $null } -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted)
Assert-True ([bool]$e115End.ok) ('the success-end close disables and persists (reason=' + [string]$e115End.reason + ')')
Assert-Equal $run115.State.DisableCount 1 'the success-end close disables the fixed SID EXACTLY once'
Assert-True (-not $run115.State.Enabled) 'the success-end close reads Enabled=False back'
$recJsonEnd = [string](Invoke-D4File -Boundary $e115.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonEnd -match '"status"\s*:\s*"disabled"') 'the success-end close persists the disabled terminal'
Assert-True ($recJsonEnd -match '"task_register_result"\s*:\s*"registered"') 'the original track (registered) survives the close record'
# C19: §116.4 - after an INTERRUPTED record refuses a second attempt, the
# bounded close still completes: zero reset/enable + one bounded disable.
$e115c2 = Invoke-D4RecoveryClose -Contract $d115Contract -RunId $d115NewRun `
    -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] `
    -FixedSid $d115Fixed -RecoveryStatePath $d115RecPath `
    -PackageHash $d115Pkg -PlanIdentity $d115Plan `
    -AccountRunner $run115j -FileBoundary $e115j2.Boundary `
    -StoreProbe { param([string]$Path) return $null } -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted)
Assert-True ([bool]$e115c2.ok) ('the repeated interrupted-record close completes (reason=' + [string]$e115c2.reason + ')')
Assert-Equal $run115j.State.ResetCount 0 'the repeated close performs ZERO resets'
Assert-Equal $run115j.State.EnableCount 0 'the repeated close performs ZERO enables'
Assert-Equal $run115j.State.DisableCount 2 'the re-entry close + the explicit repeated close = TWO bounded disables (idempotent, no re-enable)'
Assert-True (-not $run115j.State.Enabled) 'repeated closing NEVER re-enables the account'
$recJson19 = [string](Invoke-D4File -Boundary $e115j2.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJson19 -match '"status"\s*:\s*"disabled"') 'the interrupted close persists the disabled terminal'
# but a FRESH attempt still requires the clean probe (C8 covers the busy one);
# assert the close of the C14 slice with a fresh busy env refuses ZERO actions.
$t115n = @{}
$run115n = New-D4FakeRecoveryRunner -TaskModel $t115n -FixedSid $d115Fixed
$e115n = New-D115Env -OldJson $oldJson115 -TaskModel $t115n -Runner $run115n `
    -OccProbe $d115OccBusy -AclEdge $d115AclTrusted
Assert-True (-not $e115n.Result.Ok) 'a FRESH recovery attempt STILL requires the clean occupancy probe'
Assert-Equal $run115n.State.ResetCount 0 'the fresh-busy refusal performs ZERO resets'
Assert-Equal $run115n.State.RegisterCount 0 'the fresh-busy refusal performs ZERO registrations'
# C12: the REAL runner ops address Set-LocalUser / Enable-LocalUser BY SID
#      (never by name) - fake cmdlet boundaries + counters.
$d115SetSids = New-Object System.Collections.Generic.List[string]
$d115EnableSids = New-Object System.Collections.Generic.List[string]
function script:Set-LocalUser {
    param([string]$SID, [System.Security.SecureString]$Password)
    $d115SetSids.Add([string]$SID)
    return $null
}
function script:Enable-LocalUser {
    param([string]$SID)
    $d115EnableSids.Add([string]$SID)
    return $null
}
function script:Get-LocalUser {
    # THREE shapes: enumeration (no args - the §113 recovery by-SID query),
    # by-SID, by-name - the model contains exactly the fixed-SID user.
    param([string]$Name, [string]$SID)
    if ($SID) {
        if ($SID -eq 'S-1-5-21-777-77-1007') {
            return [pscustomobject]@{ Name = 'um_share_d4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-1007' }; Enabled = $true }
        }
        return $null
    }
    if ($Name) {
        if ($Name -eq 'um_share_d4') {
            return [pscustomobject]@{ Name = 'um_share_d4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-1007' }; Enabled = $true }
        }
        return $null
    }
    # ENUMERATION shape (no params): the full collection
    return [object[]]@([pscustomobject]@{ Name = 'um_share_d4'; SID = [pscustomobject]@{ Value = 'S-1-5-21-777-77-1007' }; Enabled = $true })
}
function script:Get-LocalGroupMember {
    param([string]$SID)
    return @()
}
$runner115real = New-D4AccountRunner
$resResetOp = & $runner115real['ResetAccountPasswordBySid'] 'S-1-5-21-777-77-1007' ([System.Security.SecureString]::new()) @{}
Assert-True ([bool]$resResetOp.ok) 'the ResetAccountPasswordBySid op succeeds through the fake cmdlet boundary'
Assert-Equal ([string]$d115SetSids[0]) 'S-1-5-21-777-77-1007' 'Set-LocalUser is addressed BY SID (never a name)'
$resEnableOp = & $runner115real['EnableAccountBySid'] 'S-1-5-21-777-77-1007' @{}
Assert-True ([bool]$resEnableOp.ok) 'the EnableAccountBySid op succeeds through the fake cmdlet boundary'
Assert-Equal ([string]$d115EnableSids[0]) 'S-1-5-21-777-77-1007' 'Enable-LocalUser is addressed BY SID (never a name)'
$resSidQ = & $runner115real['QueryAccountBySid'] 'S-1-5-21-777-77-1007' @{}
Assert-True ([bool]$resSidQ.present) 'QueryAccountBySid finds the exact-SID account'
Remove-Item function:Set-LocalUser, function:Enable-LocalUser, function:Get-LocalUser, function:Get-LocalGroupMember -ErrorAction SilentlyContinue
# C13: XML declaration light contract assertion (in-memory UTF-16).
$d115Def = New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $d115Fixed } `
    -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
Assert-True ([string]$d115Def -match '^<\?xml version="1.0" encoding="UTF-16"\?>') 'the share task definition declares UTF-16 in memory (real parser contract)'


# ---- §116.5: ENTRY-LEVEL recovery consumer chain ----
Write-Host '== §116.5: fresh T0R via the ENTRY + S0 recovery-record gate =='
# Fresh T0R through the entry (-Stage T0R): plan WITH a recovery section,
# old record present (hash = the frozen baseline), account disabled runner.
$t0rOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$t0rSch = New-D4FakeSchtasks -Oses $t0rOs
$t0rTaskModel = $t0rSch.State['Tasks']
$t0rAcct = New-D4FakeRecoveryRunner -TaskModel $t0rTaskModel -FixedSid $d115Fixed
$t0rNet = New-D4EmptyRunner -Tag 'net'
$t0rProc = New-D4EmptyRunner -Tag 'proc'
$t0rAcl = New-D4HarnessAclRunner -Oses $t0rOs
$t0rStore = (New-D4FakeStoreGuard).Guard
$t0rRules = New-D4FakeRuleStore -CreatedFacts @{}
$t0rOldJson = New-D4OldRunJson -RunId $d115OldRun -Sid $d115Fixed
$t0rOldPath = Join-Path 'C:\ProgramData\UMShareExp-bootstrap' ('bootstrap-state-' + $d115OldRun + '.json')
Invoke-D4File -Boundary $t0rOs.Boundary -Operation 'WriteText' -Arguments @($t0rOldPath, $t0rOldJson)
$t0rOldHash = [string](Invoke-D4File -Boundary $t0rOs.Boundary -Operation 'Hash' -Arguments @($t0rOldPath))
$t0rRuleIds = @('47114683-9e4f-4269-a29d-7153c9c9db2a', 'e68a30eb-fce8-40af-9383-db03ce4e5382', '7182e29f-9c78-4e94-b397-091e29795b08')
$t0rWfpEmpty = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0" encoding="UTF-8"?><wfpdiag><filters/></wfpdiag>')) }
$t0rPipeEmpty = { return @() }
$t0rPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\t0r-plan.json'
$t0rPlanJson = (@{
    run_id = $acctRunId; account_name = 'um_share_d4'
    python_home = $rtDir; runtime_source_dir = $rtDir
    runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
    rule_ids = $t0rRuleIds
    recovery = @{ old_run_id = $d115OldRun; fixed_sid = $d115Fixed; created_state_sha256 = $t0rOldHash }
} | ConvertTo-Json -Depth 6)
Invoke-D4File -Boundary $t0rOs.Boundary -Operation 'WriteText' -Arguments @($t0rPlanPath, $t0rPlanJson)
$t0rEmitCfg = 'C:\ProgramData\UMShareExp-bootstrap\t0r-emitted-config.json'
$t0rCfgReal = Join-Path $tmpBase 't0r-entry-config.json'
$t0rSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $t0rEmitCfg
    RecoveryOldRunId = $d115OldRun
    RecoveryFixedSid = $d115Fixed
    SchtasksRunner = $t0rSch.Runner
    NetStatRunner = $t0rNet.Runner
    ProcessEvidenceRunner = $t0rProc.Runner
    WfpEvidenceRunner = $t0rWfpEmpty
    PipeEvidenceRunner = $t0rPipeEmpty
    FileBoundary = $t0rOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $t0rAcl.Runner
    RulesRunner = $t0rRules.Runner
    AccountRunner = $t0rAcct
    BootstrapStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
    BootstrapPlanPath = $t0rPlanPath
    BootstrapStoreGuard = $t0rStore
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
& $entry -Stage 'T0R' @t0rSplat | Out-Null
Assert-Equal $LASTEXITCODE 0 'the ENTRY fresh T0R exits 0'
Assert-Equal $t0rAcct.State.ResetCount 1 'the ENTRY T0R resets the password EXACTLY once'
Assert-Equal $t0rAcct.State.EnableCount 1 'the ENTRY T0R enables EXACTLY once'
Assert-Equal $t0rAcct.State.RegisterCount 1 'the ENTRY T0R registers EXACTLY once'
$t0rRecPath = Get-D4RecoveryStatePath -Contract $contract -RunId $acctRunId
Assert-True ($t0rOs.State['Files'].ContainsKey($t0rRecPath.ToLowerInvariant())) 'the ENTRY T0R wrote the recovery record into the protected store'
$t0rRecText = [string](Invoke-D4File -Boundary $t0rOs.Boundary -Operation 'ReadText' -Arguments @($t0rRecPath))
Assert-True ($t0rRecText -match '"status"\s*:\s*"task-registered"') 'the ENTRY T0R recovery record is task-registered'
Assert-True ($t0rRecText -match [regex]::Escape($t0rOldHash)) 'the ENTRY T0R record binds the frozen old hash'
Assert-True ($t0rOs.State['Files'].ContainsKey($t0rEmitCfg.ToLowerInvariant())) 'the ENTRY T0R emitted the SID-bearing config'
Assert-True ($t0rRecText -notmatch 'password') 'the recovery record carries NO credential'
# §116.3: an occupancy TASK QUERY FAILURE (exit != 0, NOT 'cannot find')
# through the ENTRY refuses BEFORE any reset/enable - zero side effects.
$t0rErrSch = {
    param([string[]]$argv)
    return (New-TestRunResult -ExitCode 2 -Output @('simulated scheduler query error'))
}
$t0rErrAcct = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed -TaskQueryUnconfirmed
$t0rErrPlanPath = 'C:\ProgramData\UMShareExp-bootstrap\t0r-err-plan.json'
Invoke-D4File -Boundary $t0rOs.Boundary -Operation 'WriteText' -Arguments @($t0rErrPlanPath, $t0rPlanJson)
$t0rErrSplat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = 'C:\ProgramData\UMShareExp-bootstrap\t0r-err-emit.json'
    RecoveryOldRunId = $d115OldRun
    RecoveryFixedSid = $d115Fixed
    SchtasksRunner = $t0rErrSch
    NetStatRunner = $t0rNet.Runner
    ProcessEvidenceRunner = $t0rProc.Runner
    WfpEvidenceRunner = $t0rWfpEmpty
    PipeEvidenceRunner = $t0rPipeEmpty
    FileBoundary = $t0rOs.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $t0rAcl.Runner
    RulesRunner = $t0rRules.Runner
    AccountRunner = $t0rErrAcct
    BootstrapStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
    BootstrapPlanPath = $t0rErrPlanPath
    BootstrapStoreGuard = $t0rStore
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
$t0rErrThrew = $false
try { & $entry -Stage 'T0R' @t0rErrSplat | Out-Null; $t0rErrThrew = ($LASTEXITCODE -ne 0) } catch { $t0rErrThrew = $true }
Assert-True $t0rErrThrew 'an ENTRY T0R with a task-query FAILURE is refused'
Assert-Equal $t0rErrAcct.State.ResetCount 0 'the task-query-failure refusal performs ZERO resets'
Assert-Equal $t0rErrAcct.State.EnableCount 0 'the task-query-failure refusal performs ZERO enables'
Assert-Equal $t0rErrAcct.State.RegisterCount 0 'the task-query-failure refusal performs ZERO registrations'
# §116.2: the S0 run-owned task exemption for recovery requires the RECOVERY
# leaf's OWN trust - an untrusted recovery leaf grants NO exemption, so a
# present share task stays a FOREIGN occupant and S0 refuses.
$s0os = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$s0sch = New-D4FakeSchtasks -Oses $s0os
$s0acct = New-D4FakeRecoveryRunner -TaskModel $s0sch.State['Tasks'] -FixedSid $d115Fixed
$s0net = New-D4EmptyRunner -Tag 'net'
$s0proc = New-D4EmptyRunner -Tag 'proc'
$s0acl = New-D4HarnessAclRunner -Oses $s0os
$s0surRules = New-D4FakeRuleStore -CreatedFacts @{}
$s0old = New-D4OldRunJson -RunId $d115OldRun -Sid $d115Fixed
$s0oldPath = Join-Path 'C:\ProgramData\UMShareExp-bootstrap' ('bootstrap-state-' + $d115OldRun + '.json')
Invoke-D4File -Boundary $s0os.Boundary -Operation 'WriteText' -Arguments @($s0oldPath, $s0old)
$s0oldHash = [string](Invoke-D4File -Boundary $s0os.Boundary -Operation 'Hash' -Arguments @($s0oldPath))
# real config path (the entry's config reader is a REAL-path consumer) -
# built with the SAME reviewed emitter the T0 path uses (shape-guaranteed).
$s0cfgObj = New-D4ExperimentConfig -Contract $contract -RunId $acctRunId `
    -OperatorAccount 'fixture-operator' -OperatorSid 'S-1-5-21-777-77-1001' `
    -ShareAccount 'um_share_d4' -ShareSid $d115Fixed `
    -PythonHome $rtDir -RuntimeSourceDir $rtDir `
    -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) -RuleIds $t0rRuleIds
$s0CfgReal = Join-Path $tmpBase 's0-recovery-entry-config.json'
[IO.File]::WriteAllText($s0CfgReal, ($s0cfgObj | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
# the recovery record binds the SAME package + plan identity the entry's
# ownShare read will demand from the collected config - never hand-written.
$s0cfgPlanIdentity = Get-D4BootstrapPlanIdentity -RunId $acctRunId `
    -AccountName 'um_share_d4' -TaskName ([string]$contract['ShareTaskName']) `
    -PythonHome ([string]$s0cfgObj['python_home']) -RuntimeSourceDir ([string]$s0cfgObj['runtime_source_dir']) `
    -RuntimeFiles @($s0cfgObj['runtime_files']) `
    -RuleMap (Get-D4RuleRoleMap -Rules @($s0cfgObj['limit_rules']))
$s0recPath = Get-D4RecoveryStatePath -Contract $contract -RunId $acctRunId
$s0recObj = @{
    schema = 'd4-account-recovery'; run_id = $acctRunId; account_name = 'um_share_d4'
    task_name = $contract['ShareTaskName']; created_state_path = $s0oldPath
    created_state_sha256 = $s0oldHash; created_sid = $d115Fixed
    package_hash = $pwBootstrapIdentity; plan_identity = $s0cfgPlanIdentity
    status = 'task-registered'; intent_at = 't'; reset_at = 't'; enabled_at = 't'
    task_registered_at = 't'; disabled_at = ''; unconfirmed_reason = ''; task_register_result = 'registered'
}
Invoke-D4File -Boundary $s0os.Boundary -Operation 'WriteText' -Arguments @($s0recPath, ($s0recObj | ConvertTo-Json -Depth 5))
$s0tasks = $s0sch.State['Tasks']
$s0defXml = New-D4ShareTaskDefinitionXml -Contract $contract -Config @{ share_sid = $d115Fixed } `
    -CommandPath (Join-Path $contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
$s0tasks[$contract['ShareTaskName'].ToLowerInvariant()] = $s0defXml
$s0Splat = @{
    ExecuteExperiment = $true
    TestHarness = $true
    ApprovedRunId = $acctRunId
    ConfigPath = $s0CfgReal
    SchtasksRunner = $s0sch.Runner
    NetStatRunner = $s0net.Runner
    ProcessEvidenceRunner = $s0proc.Runner
    FileBoundary = $s0os.Boundary
    BrokerInstaller = $brokerInstaller
    StoreFactory = $storeFactory
    IcaclsRunner = $s0acl.Runner
    RulesRunner = $s0surRules.Runner
    AccountRunner = $s0acct
    BootstrapStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
    BootstrapAclProbe = $script:r97TrustedAclProbe
    BootstrapOperatorAccount = 'fixture-operator'
    BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
}
# negative: the recovery leaf is UNTRUSTED (discriminating edge) -> no exemption
$s0TrustedRoot = (New-D4FakeGetAclEdge -AlwaysTrusted).Edge
$s0EdgeBadRec = (New-D4FakeGetAclEdge -DescriptorByPath @{
    $s0recPath.ToLowerInvariant() = $null
}).Edge
$s0SplatBad = $s0Splat.Clone()
$s0SplatBad['BootstrapAclProbe'] = (New-D4RealAclProbe -GetAclEdge $s0EdgeBadRec)
$s0ThrewBad = $false
try { & $entry -Stage 'S0' @s0SplatBad | Out-Null; $s0ThrewBad = ($LASTEXITCODE -ne 0) } catch { $s0ThrewBad = $true }
Assert-True $s0ThrewBad 'S0 refuses when the RECOVERY leaf is NOT trusted (no exemption; the task stays foreign)'
# positive: the recovery leaf is TRUSTED -> the full-shape exemption is granted -> S0 exits 0
$s0SplatGood = $s0Splat.Clone()
$s0SplatGood['BootstrapAclProbe'] = $script:r97TrustedAclProbe
& $entry -Stage 'S0' @s0SplatGood | Out-Null
Assert-Equal $LASTEXITCODE 0 'S0 accepts its own recovery-registered task when the recovery leaf IS trusted (full-shape exemption)'


# ---- §116.4: FileBoundary-injected accounting write failures still close ----
function New-D115FailAtWriteBoundary {
    # Wraps an in-memory OS boundary so the Nth WriteText to the RECOVERY
    # record path throws exactly once (then delegates normally) - the harness
    # for "the accounting write failed after a side effect" (§116.4).
    # Explicit per-op signatures: a single [object[]] param collapses a
    # splatted argument list (only the first element survives), so each op
    # declares its real parameters and delegates positionally.
    param([hashtable]$Base, [int]$FailWrite = 1)
    $st = @{ N = 0; FailWrite = $FailWrite; Failed = $false }
    return @{
        Exists    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Exists' -Arguments @($Path)) }.GetNewClosure()
        IsLeaf    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'IsLeaf' -Arguments @($Path)) }.GetNewClosure()
        List      = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'List' -Arguments @($Path)) }.GetNewClosure()
        ReadText  = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'ReadText' -Arguments @($Path)) }.GetNewClosure()
        WriteXml  = { param([string]$Path, [string]$Content) return (Invoke-D4File -Boundary $Base -Operation 'WriteXml' -Arguments @($Path, $Content)) }.GetNewClosure()
        WriteText = {
            param([string]$Path, [string]$Content)
            if (-not $st['Failed'] -and $Path -eq $d115RecPath) {
                $st['N']++
                if ($st['N'] -eq $st['FailWrite']) {
                    $st['Failed'] = $true
                    throw ('simulated accounting write failure at write #' + $st['N'] + ' to ' + $Path)
                }
            }
            return (Invoke-D4File -Boundary $Base -Operation 'WriteText' -Arguments @($Path, $Content))
        }.GetNewClosure()
        MakeDir   = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'MakeDir' -Arguments @($Path)) }.GetNewClosure()
        Remove    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Remove' -Arguments @($Path)) }.GetNewClosure()
        CopyBytes = { param([string]$Source, [string]$Destination) return (Invoke-D4File -Boundary $Base -Operation 'CopyBytes' -Arguments @($Source, $Destination)) }.GetNewClosure()
        Hash      = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Hash' -Arguments @($Path)) }.GetNewClosure()
    }
}
# C20: the RESET-accounting write fails -> the bounded close still disables
# and the record ends disabled with the original reason.
$t115w1 = @{}
$run115w1 = New-D4FakeRecoveryRunner -TaskModel $t115w1 -FixedSid $d115Fixed
$e115w1 = New-D115Env -OldJson $oldJson115 -TaskModel $t115w1 -Runner $run115w1 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted `
    -WrapBoundary { param($b) New-D115FailAtWriteBoundary -Base $b -FailWrite 2 }
Assert-True (-not $e115w1.Result.Ok) 'a RESET-accounting write failure refuses the recovery'
Assert-Equal $run115w1.State.DisableCount 1 'the reset-accounting failure still runs the bounded disable'
Assert-True (-not $run115w1.State.Enabled) 'the reset-accounting failure reads Enabled=False back'
$recJsonW1 = [string](Invoke-D4File -Boundary $e115w1.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonW1 -match '"status"\s*:\s*"disabled"') 'the reset-accounting failure ends the record disabled'
# C21: the ENABLED-accounting write fails -> the bounded close still disables.
$t115w2 = @{}
$run115w2 = New-D4FakeRecoveryRunner -TaskModel $t115w2 -FixedSid $d115Fixed
$e115w2 = New-D115Env -OldJson $oldJson115 -TaskModel $t115w2 -Runner $run115w2 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted `
    -WrapBoundary { param($b) New-D115FailAtWriteBoundary -Base $b -FailWrite 3 }
Assert-True (-not $e115w2.Result.Ok) 'an ENABLED-accounting write failure refuses the recovery'
Assert-Equal $run115w2.State.DisableCount 1 'the enabled-accounting failure still runs the bounded disable'
Assert-True (-not $run115w2.State.Enabled) 'the enabled-accounting failure reads Enabled=False back'
$recJsonW2 = [string](Invoke-D4File -Boundary $e115w2.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonW2 -match '"status"\s*:\s*"disabled"') 'the enabled-accounting failure ends the record disabled'
# C22: the TASK-REGISTERED (final) accounting write fails -> register happened,
# the bounded close still disables, zero re-register.
$t115w3 = @{}
$run115w3 = New-D4FakeRecoveryRunner -TaskModel $t115w3 -FixedSid $d115Fixed
$e115w3 = New-D115Env -OldJson $oldJson115 -TaskModel $t115w3 -Runner $run115w3 `
    -OccProbe $d115OccClean -AclEdge $d115AclTrusted `
    -WrapBoundary { param($b) New-D115FailAtWriteBoundary -Base $b -FailWrite 4 }
Assert-True (-not $e115w3.Result.Ok) 'a final task-registered accounting write failure refuses (with close)'
Assert-Equal $run115w3.State.RegisterCount 1 'the final-accounting case DID register once'
Assert-Equal $run115w3.State.DisableCount 1 'the final-accounting failure still runs the bounded disable'
Assert-True (-not $run115w3.State.Enabled) 'the final-accounting failure reads Enabled=False back'
$recJsonW3 = [string](Invoke-D4File -Boundary $e115w3.Os.Boundary -Operation 'ReadText' -Arguments @($d115RecPath))
Assert-True ($recJsonW3 -match '"status"\s*:\s*"disabled"') 'the final-accounting failure ends the record disabled'
Assert-True ($recJsonW3 -match 'task-registered accounting write failed') 'the final-accounting failure reason is preserved'


# ---- §118: full tri-state occupancy + unified closing via the REAL entry ----
Write-Host '== §118: entry-level occupancy tri-state + unified close =='
function New-D118Base {
    # Fresh in-memory store + old record + frozen plan (with recovery section).
    $osI = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
    $oldI = New-D4OldRunJson -RunId $d115OldRun -Sid $d115Fixed
    $oldPathI = Join-Path 'C:\ProgramData\UMShareExp-bootstrap' ('bootstrap-state-' + $d115OldRun + '.json')
    Invoke-D4File -Boundary $osI.Boundary -Operation 'WriteText' -Arguments @($oldPathI, $oldI)
    $oldHashI = [string](Invoke-D4File -Boundary $osI.Boundary -Operation 'Hash' -Arguments @($oldPathI))
    $planPathI = 'C:\ProgramData\UMShareExp-bootstrap\d118-plan.json'
    $planJsonI = (@{
        run_id = $acctRunId; account_name = 'um_share_d4'
        python_home = $rtDir; runtime_source_dir = $rtDir
        runtime_files = @(@{ file = 'python-core.bin'; sha256 = $rtSha })
        rule_ids = $t0rRuleIds
        recovery = @{ old_run_id = $d115OldRun; fixed_sid = $d115Fixed; created_state_sha256 = $oldHashI }
    } | ConvertTo-Json -Depth 6)
    Invoke-D4File -Boundary $osI.Boundary -Operation 'WriteText' -Arguments @($planPathI, $planJsonI)
    $schI = New-D4FakeSchtasks -Oses $osI
    return @{ Os = $osI; OldPath = $oldPathI; OldHash = $oldHashI; PlanPath = $planPathI
              Emit = 'C:\ProgramData\UMShareExp-bootstrap\d118-emit.json'; Sch = $schI }
}
function New-D118Splat {
    param([hashtable]$Os, [string]$PlanPath, [string]$Emit, [hashtable]$Runner,
          [scriptblock]$Net, [scriptblock]$Proc, [scriptblock]$Wfp, [scriptblock]$Pipe,
          [object]$Schtasks, [switch]$SkipWfp, [switch]$SkipPipe)
    $h = @{
        ExecuteExperiment = $true; TestHarness = $true
        ApprovedRunId = $acctRunId; ConfigPath = $Emit
        RecoveryOldRunId = $d115OldRun; RecoveryFixedSid = $d115Fixed
        SchtasksRunner = $Schtasks.Runner
        NetStatRunner = $Net; ProcessEvidenceRunner = $Proc
        FileBoundary = $Os.Boundary
        BrokerInstaller = $brokerInstaller; StoreFactory = $storeFactory
        IcaclsRunner = $t0rAcl.Runner; RulesRunner = $t0rRules.Runner
        AccountRunner = $Runner
        BootstrapStatePath = Get-D4BootstrapStatePath -Contract $contract -RunId $acctRunId
        BootstrapPlanPath = $PlanPath
        BootstrapStoreGuard = $t0rStore
        BootstrapAclProbe = $script:r97TrustedAclProbe
        BootstrapOperatorAccount = 'fixture-operator'; BootstrapOperatorSid = 'S-1-5-21-777-77-1001'
    }
    if (-not $SkipWfp) { $h['WfpEvidenceRunner'] = $Wfp }
    if (-not $SkipPipe) { $h['PipeEvidenceRunner'] = $Pipe }
    return $h
}
$d118ProcEmpty = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('')) }
$d118ProcHit = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('[{"ProcessId":9,"CommandLine":"C:\\ProgramData\\UMShareExp-broker\\probe","ExecutablePath":"C:\\ProgramData\\UMShareExp-broker\\um-share-python.exe"}]')) }
$d118WfpOk = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0" encoding="UTF-8"?><wfpdiag><filters/></wfpdiag>')) }
$d118WfpGarbage = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('this is NOT an xml dump at all')) }
$d118NetOk = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('')) }
$d118NetBad = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('garbage no rows')) }
$d118PipeOk = { return @() }
# B1: a task ENUMERATION query failure whose message CONTAINS 找不到/未找到 is
# UNCONFIRMED through the real entry - zero reset/enable (never 'absent').
$b1 = New-D118Base
$b1run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed -TaskQueryUnconfirmed
$b1run.State['TaskModel'] = @{}
$oldT115 = $b1run.State
# (New-D4FakeRecoveryRunner has no TaskModel param - use empty; QueryTask
#  unconfirmed is what we need here.)
$b1s = New-D118Splat -Os $b1.Os -PlanPath $b1.PlanPath -Emit $b1.Emit -Runner $b1run `
    -Net $d118NetOk -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $b1.Sch
$b1Threw = $false
try { & $entry -Stage 'T0R' @b1s | Out-Null; $b1Threw = ($LASTEXITCODE -ne 0) } catch { $b1Threw = $true }
Assert-True $b1Threw 'an ENTRY T0R with a task-enumeration UNCONFIRMED query (message containing 未找到) is refused'
Assert-Equal $b1run.State.ResetCount 0 'the task-unconfirmed refusal performs ZERO resets'
Assert-Equal $b1run.State.EnableCount 0 'the task-unconfirmed refusal performs ZERO enables'
# B2: exit-0 NON-EMPTY BAD WFP output (not XML) -> unconfirmed, zero actions.
$b2 = New-D118Base
$b2run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$b2s = New-D118Splat -Os $b2.Os -PlanPath $b2.PlanPath -Emit $b2.Emit -Runner $b2run `
    -Net $d118NetOk -Proc $d118ProcEmpty -Wfp $d118WfpGarbage -Pipe $d118PipeOk -Schtasks $b2.Sch
$b2Threw = $false
try { & $entry -Stage 'T0R' @b2s | Out-Null; $b2Threw = ($LASTEXITCODE -ne 0) } catch { $b2Threw = $true }
Assert-True $b2Threw 'an ENTRY T0R with exit-0 BAD WFP output is refused (structured parse -> UNCONFIRMED)'
Assert-Equal $b2run.State.ResetCount 0 'the bad-WFP refusal performs ZERO resets'
Assert-Equal $b2run.State.EnableCount 0 'the bad-WFP refusal performs ZERO enables'
# B3: bad (non-parseable) netstat output -> unconfirmed, zero actions.
$b3 = New-D118Base
$b3run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$b3s = New-D118Splat -Os $b3.Os -PlanPath $b3.PlanPath -Emit $b3.Emit -Runner $b3run `
    -Net $d118NetBad -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $b3.Sch
$b3Threw = $false
try { & $entry -Stage 'T0R' @b3s | Out-Null; $b3Threw = ($LASTEXITCODE -ne 0) } catch { $b3Threw = $true }
Assert-True $b3Threw 'an ENTRY T0R with bad netstat output is refused (bad output -> UNCONFIRMED)'
Assert-Equal $b3run.State.ResetCount 0 'the bad-netstat refusal performs ZERO resets'
# B4: an EXPERIMENT process (image/CL under the experiment root via the real
#     command-line evidence adapter) -> refused, zero actions.
$b4 = New-D118Base
$b4run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$b4s = New-D118Splat -Os $b4.Os -PlanPath $b4.PlanPath -Emit $b4.Emit -Runner $b4run `
    -Net $d118NetOk -Proc $d118ProcHit -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $b4.Sch
$b4Threw = $false
try { & $entry -Stage 'T0R' @b4s | Out-Null; $b4Threw = ($LASTEXITCODE -ne 0) } catch { $b4Threw = $true }
Assert-True $b4Threw 'an ENTRY T0R with an experiment process present (image/CL under the experiment root) is refused'
Assert-Equal $b4run.State.ResetCount 0 'the experiment-process refusal performs ZERO resets'
Assert-Equal $b4run.State.EnableCount 0 'the experiment-process refusal performs ZERO enables'
# B5: a MISSING required evidence runner (WFP) refuses via the harness gate -
#     never skips into a 'clean' verdict.
$b5 = New-D118Base
$b5run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$b5s = New-D118Splat -Os $b5.Os -PlanPath $b5.PlanPath -Emit $b5.Emit -Runner $b5run `
    -Net $d118NetOk -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $b5.Sch -SkipWfp
$b5Threw = $false
try { & $entry -Stage 'T0R' @b5s | Out-Null; $b5Threw = ($LASTEXITCODE -ne 0) } catch { $b5Threw = $true }
Assert-True $b5Threw 'an ENTRY T0R with a MISSING WFP evidence runner is refused (never skipped to clean)'
Assert-Equal $b5run.State.ResetCount 0 'the missing-runner refusal performs ZERO resets'
# C1: §118.2 - an INTERRUPTED (enabled) record re-entered through the REAL entry:
#     zero reset/enable, ONE bounded disable, same-SID disabled post-verify.
$c1 = New-D118Base
$c1run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed -AccountEnabled $true
$d118PlanId = Get-D4BootstrapPlanIdentity -RunId $acctRunId `
    -AccountName 'um_share_d4' -TaskName ([string]$contract['ShareTaskName']) `
    -PythonHome $rtDir -RuntimeSourceDir $rtDir `
    -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap (Get-D4RuleRoleMap -OrderedIds $t0rRuleIds)
$c1RecPath = Get-D4RecoveryStatePath -Contract $contract -RunId $acctRunId
$c1Rec = (@{
    schema = 'd4-account-recovery'; run_id = $acctRunId; account_name = 'um_share_d4'
    task_name = $contract['ShareTaskName']; created_state_path = $c1.OldPath
    created_state_sha256 = $c1.OldHash; created_sid = $d115Fixed
    package_hash = $pwBootstrapIdentity; plan_identity = $d118PlanId
    status = 'enabled'; intent_at = 't'; reset_at = 't'; enabled_at = 't'
    task_registered_at = ''; disabled_at = ''; unconfirmed_reason = ''; task_register_result = ''
} | ConvertTo-Json -Depth 5)
Invoke-D4File -Boundary $c1.Os.Boundary -Operation 'WriteText' -Arguments @($c1RecPath, $c1Rec)
$c1s = New-D118Splat -Os $c1.Os -PlanPath $c1.PlanPath -Emit $c1.Emit -Runner $c1run `
    -Net $d118NetOk -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $c1.Sch
$c1Threw = $false; $c1Err = ''
try { & $entry -Stage 'T0R' @c1s | Out-Null; $c1Threw = ($LASTEXITCODE -ne 0) } catch { $c1Threw = $true; $c1Err = $_.Exception.Message }
Assert-True $c1Threw 'an INTERRUPTED (enabled) recovery record re-entered via the ENTRY is refused (no second attempt)'
Assert-Equal $c1run.State.ResetCount 0 'the interrupted re-entry performs ZERO resets'
Assert-Equal $c1run.State.EnableCount 0 'the interrupted re-entry performs ZERO enables'
Assert-Equal $c1run.State.DisableCount 1 'the interrupted re-entry performs ONE bounded disable'
Assert-True (-not $c1run.State.Enabled) 'the interrupted re-entry reads the same SID disabled'
$c1RecJson = [string](Invoke-D4File -Boundary $c1.Os.Boundary -Operation 'ReadText' -Arguments @($c1RecPath))
Assert-True ($c1RecJson -match '"status"\s*:\s*"disabled"') 'the interrupted re-entry persists the disabled terminal'
# C2: §118.2 - a CONFIG-WRITE failure AFTER a successful recovery runs the SAME
#     bounded close through the entry (account enabled + task registered get
#     cleaned up; the close outcome is visible in the thrown error).
$c2 = New-D118Base
$c2run = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$osC = $c2.Os
function New-D118FailEmitBoundary {
    param([hashtable]$Base, [string]$EmitPath)
    return @{
        Exists    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Exists' -Arguments @($Path)) }.GetNewClosure()
        IsLeaf    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'IsLeaf' -Arguments @($Path)) }.GetNewClosure()
        List      = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'List' -Arguments @($Path)) }.GetNewClosure()
        ReadText  = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'ReadText' -Arguments @($Path)) }.GetNewClosure()
        WriteXml  = { param([string]$Path, [string]$Content) return (Invoke-D4File -Boundary $Base -Operation 'WriteXml' -Arguments @($Path, $Content)) }.GetNewClosure()
        WriteText = {
            param([string]$Path, [string]$Content)
            if ($Path -eq $EmitPath) { throw 'simulated config emit failure' }
            return (Invoke-D4File -Boundary $Base -Operation 'WriteText' -Arguments @($Path, $Content))
        }.GetNewClosure()
        MakeDir   = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'MakeDir' -Arguments @($Path)) }.GetNewClosure()
        Remove    = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Remove' -Arguments @($Path)) }.GetNewClosure()
        CopyBytes = { param([string]$Source, [string]$Destination) return (Invoke-D4File -Boundary $Base -Operation 'CopyBytes' -Arguments @($Source, $Destination)) }.GetNewClosure()
        Hash      = { param([string]$Path) return (Invoke-D4File -Boundary $Base -Operation 'Hash' -Arguments @($Path)) }.GetNewClosure()
    }
}
$c2EmitBoundary = New-D118FailEmitBoundary -Base $osC.Boundary -EmitPath $c2.Emit
$c2s = New-D118Splat -Os $c2.Os -PlanPath $c2.PlanPath -Emit $c2.Emit -Runner $c2run `
    -Net $d118NetOk -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $c2.Sch
$c2s['FileBoundary'] = $c2EmitBoundary
$c2Err = $null; $c2Threw = $false
try { & $entry -Stage 'T0R' @c2s | Out-Null; $c2Threw = ($LASTEXITCODE -ne 0) } catch { $c2Threw = $true; $c2Err = $_.Exception.Message }
Assert-True $c2Threw 'an ENTRY T0R whose config emission fails AFTER recovery is refused'
Assert-True ([string]$c2Err -match 'recovery close') 'the config-failure error names the bounded close'
Assert-Equal $c2run.State.DisableCount 1 'the config-emission failure still disables the fixed SID EXACTLY once'
Assert-True (-not $c2run.State.Enabled) 'the config-emission failure reads Enabled=False back'
$c2RecJson = [string](Invoke-D4File -Boundary $osC.Boundary -Operation 'ReadText' -Arguments @((Get-D4RecoveryStatePath -Contract $contract -RunId $acctRunId)))
Assert-True ($c2RecJson -match '"status"\s*:\s*"disabled"') 'the config-emission failure persists the disabled terminal'


# C23: §118.2 - a record ALREADY 'disabled' (post-verified) re-entered returns
#     idempotently: zero reset/enable/disable, no re-rotation, no re-enable.
$t115z2 = @{}
$run115z2 = New-D4FakeRecoveryRunner -TaskModel $t115z2 -FixedSid $d115Fixed
$alrIcOs = New-D4InMemoryOs -Root $d115Root
Invoke-D4File -Boundary $alrIcOs.Boundary -Operation 'WriteText' -Arguments @($d115OldPath, $oldJson115)
$alrIcSha = [string](Invoke-D4File -Boundary $alrIcOs.Boundary -Operation 'Hash' -Arguments @($d115OldPath))
$alrIcRec = (@{
    schema = 'd4-account-recovery'; run_id = $d115NewRun; account_name = 'um_share_d4'
    task_name = $contract['ShareTaskName']; created_state_path = $d115OldPath
    created_state_sha256 = $alrIcSha; created_sid = $d115Fixed; package_hash = $d115Pkg
    plan_identity = $d115Plan; status = 'disabled'
    intent_at = 't'; reset_at = 't'; enabled_at = 't'; task_registered_at = ''
    disabled_at = 't'; unconfirmed_reason = 'historical'; task_register_result = ''
} | ConvertTo-Json -Depth 5)
Invoke-D4File -Boundary $alrIcOs.Boundary -Operation 'WriteText' -Arguments @($d115RecPath, $alrIcRec)
$alrDef = {
    param([string]$Sid)
    New-D4ShareTaskDefinitionXml -Contract $d115Contract -Config @{ share_sid = $Sid } `
        -CommandPath (Join-Path $d115Contract['ExperimentRoot'] 'run-live.cmd') -LogonType 'Password'
}.GetNewClosure()
$run115z2.State['Enabled'] = $false
$resZ2 = Invoke-D4AccountRecovery -Contract $d115Contract -RunId $d115NewRun `
    -AccountName 'um_share_d4' -ShareTaskName $contract['ShareTaskName'] `
    -OldRunId $d115OldRun -OldStatePath $d115OldPath -FixedSid $d115Fixed `
    -ExpectedOldStateSha256 $alrIcSha `
    -DefinitionBuilder $alrDef -PackageHash $d115Pkg -PlanIdentity $d115Plan `
    -RecoveryStatePath $d115RecPath -AccountRunner $run115z2 `
    -FileBoundary $alrIcOs.Boundary -StoreProbe { param([string]$Path) return $null } `
    -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted) -OccupancyProbe $d115OccClean
Assert-True ([bool]$resZ2.Ok) ('an already-disabled (post-verified) record re-enters idempotently: ' + ($resZ2 | ConvertTo-Json -Compress))
Assert-Equal ([string]$resZ2.Status) 'disabled' 'the idempotent re-entry reports the disabled terminal'
Assert-Equal $run115z2.State.ResetCount 0 'the already-disabled re-entry performs ZERO resets'
Assert-Equal $run115z2.State.EnableCount 0 'the already-disabled re-entry performs ZERO enables'
Assert-Equal $run115z2.State.DisableCount 0 'the already-disabled re-entry performs ZERO disables (no storm)'
Assert-True (-not $run115z2.State.Enabled) 'the already-disabled idempotent return NEVER re-enables'


# ---- §120: WFP structure / netstat row shape / disabled-terminal entry ---
Write-Host '== §120: structure-validated occupancy + disabled-terminal entry consumption =='
$d120WfpErrRoot = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0"?><error/>')) }
$d120WfpNoCol = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0"?><wfpdiag><unrelated/></wfpdiag>')) }
$d120WfpBadRec = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0"?><wfpdiag><filters><item><bogus/></item></filters></wfpdiag>')) }
$d120NetFourWords = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('a b c d')) }
$d120NetMixed = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 0.0.0.0:1234 0.0.0.0:0 ESTABLISHED 4321','garbage row here')) }
$d120NetLegal = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 4321')) }
function Invoke-D120T0R {
    param([scriptblock]$Wfp, [scriptblock]$Net, [switch]$ExpectThrow, [string]$Label)
    $b = New-D118Base
    $r = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
    $s = New-D118Splat -Os $b.Os -PlanPath $b.PlanPath -Emit $b.Emit -Runner $r `
        -Net $Net -Proc $d118ProcEmpty -Wfp $Wfp -Pipe $d118PipeOk -Schtasks $b.Sch
    $threw = $false
    try { & $entry -Stage 'T0R' @s | Out-Null; $threw = ($LASTEXITCODE -ne 0) } catch { $threw = $true }
    if ($ExpectThrow) { Assert-True $threw $Label }
    return @{ Threw = $threw; Runner = $r; Os = $b.Os; Emit = $b.Emit }
}
# D1: legal XML with the WRONG root (error) -> UNCONFIRMED, zero actions
$d1 = Invoke-D120T0R -Wfp $d120WfpErrRoot -Net $d120NetLegal -ExpectThrow 'an exit-0 legal-XML dump with the WRONG root is refused (never an empty set)'
Assert-Equal $d1.Runner.State.ResetCount 0 'the wrong-root WFP refusal performs ZERO resets'
Assert-Equal $d1.Runner.State.EnableCount 0 'the wrong-root WFP refusal performs ZERO enables'
# D2: no <filters> collection -> UNCONFIRMED
$d2 = Invoke-D120T0R -Wfp $d120WfpNoCol -Net $d120NetLegal -ExpectThrow 'a dump WITHOUT a <filters> collection is refused'
Assert-Equal $d2.Runner.State.ResetCount 0 'the no-collection WFP refusal performs ZERO resets'
# D3: a malformed <filters> record (no filterKey) -> UNCONFIRMED
$d3 = Invoke-D120T0R -Wfp $d120WfpBadRec -Net $d120NetLegal -ExpectThrow 'a malformed <filters> record (no filterKey) is refused'
Assert-Equal $d3.Runner.State.ResetCount 0 'the malformed-record WFP refusal performs ZERO resets'
# D4: four-word garbage netstat -> UNCONFIRMED
$d4 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d120NetFourWords -ExpectThrow 'exit-0 four-word garbage netstat is refused (not a legal row)'
Assert-Equal $d4.Runner.State.ResetCount 0 'the four-word netstat refusal performs ZERO resets'
# D5: legal rows MIXED with a bad row -> UNCONFIRMED (a bad row cannot hide)
$d5 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d120NetMixed -ExpectThrow 'netstat legal rows mixed with a bad row is refused'
Assert-Equal $d5.Runner.State.ResetCount 0 'the mixed-netstat refusal performs ZERO resets'
# D6: VALID empty WFP + legal (non-9100) netstat rows -> the recovery proceeds
$d6 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d120NetLegal 'valid empty WFP + legal non-9100 netstat rows let the recovery proceed'  # ExpectThrow default OFF
Assert-Equal $d6.Runner.State.ResetCount 1 'the valid-empty occupancy case resets EXACTLY once (positive)'
Assert-Equal $d6.Runner.State.EnableCount 1 'the valid-empty occupancy case enables EXACTLY once (positive)'
# D7: §120.2 - a TRUSTED 'disabled' record consumed by the ENTRY is a CLOSED
#     terminal: zero config writes, zero reset/enable/register/disable.
$b7 = New-D118Base
$r7 = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$b7RecPath = Get-D4RecoveryStatePath -Contract $contract -RunId $acctRunId
$b7PlanId = Get-D4BootstrapPlanIdentity -RunId $acctRunId `
    -AccountName 'um_share_d4' -TaskName ([string]$contract['ShareTaskName']) `
    -PythonHome $rtDir -RuntimeSourceDir $rtDir `
    -RuntimeFiles @(@{ file = 'python-core.bin'; sha256 = $rtSha }) `
    -RuleMap (Get-D4RuleRoleMap -OrderedIds $t0rRuleIds)
$b7Rec = (@{
    schema = 'd4-account-recovery'; run_id = $acctRunId; account_name = 'um_share_d4'
    task_name = $contract['ShareTaskName']; created_state_path = $b7.OldPath
    created_state_sha256 = $b7.OldHash; created_sid = $d115Fixed
    package_hash = $pwBootstrapIdentity; plan_identity = $b7PlanId
    status = 'disabled'; intent_at = 't'; reset_at = 't'; enabled_at = 't'
    task_registered_at = ''; disabled_at = 't'; unconfirmed_reason = ''; task_register_result = ''
} | ConvertTo-Json -Depth 5)
Invoke-D4File -Boundary $b7.Os.Boundary -Operation 'WriteText' -Arguments @($b7RecPath, $b7Rec)
$s7 = New-D118Splat -Os $b7.Os -PlanPath $b7.PlanPath -Emit $b7.Emit -Runner $r7 `
    -Net $d120NetLegal -Proc $d118ProcEmpty -Wfp $d118WfpOk -Pipe $d118PipeOk -Schtasks $b7.Sch
$s7Threw = $false; $s7Err = ''
try { & $entry -Stage 'T0R' @s7 | Out-Null; $s7Threw = ($LASTEXITCODE -ne 0) } catch { $s7Threw = $true; $s7Err = $_.Exception.Message }
Assert-True $s7Threw ('a TRUSTED disabled record consumed by the ENTRY stops the run (closed) instead of reporting success: ' + [string]$s7Err)
Assert-True ([string]$s7Err -match 'did not reach task-registered') 'the disabled-terminal entry refusal names task-registered'
Assert-Equal $r7.State.ResetCount 0 'the disabled-terminal entry consumption performs ZERO resets'
Assert-Equal $r7.State.EnableCount 0 'the disabled-terminal entry consumption performs ZERO enables'
Assert-Equal $r7.State.RegisterCount 0 'the disabled-terminal entry consumption performs ZERO registrations'
Assert-Equal $r7.State.DisableCount 0 'the disabled-terminal entry consumption performs ZERO disables (no storm)'
Assert-True (-not (Invoke-D4File -Boundary $b7.Os.Boundary -Operation 'IsLeaf' -Arguments @($b7.Emit))) 'the disabled-terminal consumption writes NO config'


# ---- §122: WFP field-level strictness + netstat field validation ----
Write-Host '== §122: field-validated WFP/Netstat occupancy =='
$d122g = [string]$t0rRuleIds[0]
$d122WfpBadGuid = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('<?xml version="1.0"?><wfpdiag><filters><item><filterKey>not-a-guid</filterKey></item></filters></wfpdiag>')) }
$d122WfpTwoSets = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @(('<?xml version="1.0"?><wfpdiag><filters><item><filterKey>{' + $d122g + '}</filterKey></item></filters><filters/></wfpdiag>'))) }
$d122WfpTwoKeys = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @(('<?xml version="1.0"?><wfpdiag><filters><item><filterKey>{' + $d122g + '}</filterKey><filterKey>{e68a30eb-fce8-40af-9383-db03ce4e5382}</filterKey></item></filters></wfpdiag>'))) }
$d122WfpPresent = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @(('<?xml version="1.0"?><wfpdiag><filters><item><filterKey>{' + $d122g + '}</filterKey></item></filters></wfpdiag>'))) }
$d122NetBadAddr = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP bad bad BROKEN 123')) }
$d122NetUdp = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('UDP 0.0.0.0:1234 0.0.0.0:0 1234')) }
$d122NetIpv6Hit = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP [::1]:9100 [::]:0 LISTENING 55')) }
# E1: a legal-XML dump with an ILLEGAL (non-GUID) filterKey -> UNCONFIRMED (cannot prove ABSENT either)
$e1 = Invoke-D120T0R -Wfp $d122WfpBadGuid -Net $d120NetLegal -ExpectThrow 'an illegal non-GUID filterKey is refused (cannot prove absence either)'
Assert-Equal $e1.Runner.State.ResetCount 0 'the illegal-GUID WFP refusal performs ZERO resets'
# E2: TWO <filters> collections (first holds the target) -> UNCONFIRMED (last-wins must not mask)
$e2 = Invoke-D120T0R -Wfp $d122WfpTwoSets -Net $d120NetLegal -ExpectThrow 'a dump with TWO <filters> collections is refused (ambiguous)'
Assert-Equal $e2.Runner.State.ResetCount 0 'the double-collection WFP refusal performs ZERO resets'
# E3: TWO filterKey in one <item> -> UNCONFIRMED
$e3 = Invoke-D120T0R -Wfp $d122WfpTwoKeys -Net $d120NetLegal -ExpectThrow 'a record with TWO filterKey elements is refused (ambiguous)'
Assert-Equal $e3.Runner.State.ResetCount 0 'the double-key WFP refusal performs ZERO resets'
# E4: a LEGAL present target -> occupied, refused, zero actions
$e4 = Invoke-D120T0R -Wfp $d122WfpPresent -Net $d120NetLegal -ExpectThrow 'a present experiment rule (legal GUID) refuses the recovery'
Assert-Equal $e4.Runner.State.ResetCount 0 'the present-rule refusal performs ZERO resets'
Assert-Equal $e4.Runner.State.EnableCount 0 'the present-rule refusal performs ZERO enables'
# E6: netstat address/state fields must be VALID - 'TCP bad bad BROKEN 123' is rejected
$e6 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d122NetBadAddr -ExpectThrow 'netstat with an invalid address/state field is refused (field validation)'
Assert-Equal $e6.Runner.State.ResetCount 0 'the bad-field netstat refusal performs ZERO resets'
# E7: an impossible UDP row for the TCP-only query -> rejected
$e7 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d122NetUdp -ExpectThrow 'a UDP row in the TCP-only netstat query is refused'
Assert-Equal $e7.Runner.State.ResetCount 0 'the UDP-row refusal performs ZERO resets'
# E8: a LEGAL IPv6 LISTENING on 9100 -> occupied, refused
$e8 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d122NetIpv6Hit -ExpectThrow 'a legal IPv6 LISTENING on 9100 refuses the recovery'
Assert-Equal $e8.Runner.State.ResetCount 0 'the IPv6-9100 refusal performs ZERO resets'
# E9 control: a legal IPv4 non-9100 LISTENING row + valid empty WFP -> the recovery proceeds
$e9 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d120NetLegal 'legal IPv4 non-9100 LISTENING row lets the recovery proceed (control)'
Assert-Equal $e9.Runner.State.ResetCount 1 'the valid-Ipv4-control recovery resets EXACTLY once'


# ---- §124: netstat column-strict validation (no alternation bypass) ----
Write-Host '== §124: netstat five-column strict validation =='
# Direct unit checks of the shared row validator (deterministic, not entry-only)
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP *:* garbage').Legal) '`TCP *:* garbage` (3 cols) is NOT a legal row'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 127.0.0.1:80 *:* BROKEN 123').Legal) 'a legal address with an UNKNOWN state is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 1.2.3.4:80 LISTENING 5').Legal) 'a row with a MISSING column is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 1.2.3.4:80 5.6.7.8:90 LISTENING 123 999').Legal) 'a row with an EXTRA column is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 999.1.1.1:80 0.0.0.0:0 LISTENING 5').Legal) 'an out-of-range IPv4 octet is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 1.2.3.4:99999 0.0.0.0:0 LISTENING 5').Legal) 'an out-of-range port is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'UDP 0.0.0.0:1234 0.0.0.0:0 1234').Legal) 'a UDP row is NOT legal for the TCP-only query'
# Positive controls on the row validator (real-shape IPv4/IPv6 and wildcard)
Assert-True ((Test-D4NetstatRowFacts -Row 'TCP 0.0.0.0:9100 0.0.0.0:0 LISTENING 123').Legal) 'a real-shape IPv4 LISTENING 9100 row is legal'
$rowI6 = Test-D4NetstatRowFacts -Row 'TCP [::1]:9100 [::]:0 LISTENING 55'
Assert-True ($rowI6.Legal) 'a real-shape IPv6 LISTENING 9100 row is legal'
Assert-True ($rowI6.Port9100Listening) 'the IPv6 9100 LISTENING row is flagged as the experiment listener'
$rowWild = Test-D4NetstatRowFacts -Row 'TCP 0.0.0.0:1234 *:* CLOSE_WAIT 4321'
Assert-True ($rowWild.Legal) 'a legal *:* wildcard with a KNOWN state is legal'
# Entry-level counter-examples (all UNCONFIRMED -> zero reset/enable)
$d124NetNoState = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP *:* garbage')) }
$d124NetBadState = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 127.0.0.1:80 *:* BROKEN 123')) }
$d124NetBadOctet = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 999.1.1.1:80 0.0.0.0:0 LISTENING 5')) }
$d124NetBadPort = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 1.2.3.4:99999 0.0.0.0:0 LISTENING 5')) }
$d124NetMixed = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 4321','TCP *:* garbage')) }
$d124NetLegalIpv4 = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 4321')) }
$d124NetIpv4Hit = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP 0.0.0.0:9100 0.0.0.0:0 LISTENING 123')) }
$f1 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetNoState -ExpectThrow '`TCP *:* garbage` via the ENTRY is refused (missing cols/state/PID)'
Assert-Equal $f1.Runner.State.ResetCount 0 'the no-state netstat refusal performs ZERO resets'
$f2 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetBadState -ExpectThrow 'unknown-state netstat via the ENTRY is refused'
Assert-Equal $f2.Runner.State.ResetCount 0 'the unknown-state refusal performs ZERO resets'
$f3 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetBadOctet -ExpectThrow 'an out-of-range IPv4 octet via the ENTRY is refused'
Assert-Equal $f3.Runner.State.ResetCount 0 'the bad-octet refusal performs ZERO resets'
$f4 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetBadPort -ExpectThrow 'an out-of-range port via the ENTRY is refused'
Assert-Equal $f4.Runner.State.ResetCount 0 'the bad-port refusal performs ZERO resets'
$f5 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetMixed -ExpectThrow 'legal rows mixed with a bad netstat row via the ENTRY are refused (no swallow)'
Assert-Equal $f5.Runner.State.ResetCount 0 'the mixed-netstat refusal performs ZERO resets'
$f6 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetLegalIpv4 'legal real-shape IPv4 non-9100 row lets the recovery proceed (control)'
Assert-Equal $f6.Runner.State.ResetCount 1 'the legal-Ipv4-control recovery resets EXACTLY once'
$f7 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d124NetIpv4Hit -ExpectThrow 'a real-shape IPv4 9100 LISTENING via the ENTRY is occupied (control)'
Assert-Equal $f7.Runner.State.ResetCount 0 'the IPv4-9100 occupant refusal performs ZERO resets'


# ---- §126: netstat IPv6-parse + non-negative PID value checks ----
Write-Host '== §126: IPv6 IPAddress.TryParse + non-negative PID =='
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP [::::]:80 [::]:0 ESTABLISHED 123').Legal) 'an illegal compressed IPv6 address is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP [1.2.3.4]:80 [::]:0 ESTABLISHED 123').Legal) 'an embedded IPv4 (non-IPv6-parse) address is NOT legal'
Assert-True (-not (Test-D4NetstatRowFacts -Row 'TCP 0.0.0.0:9100 0.0.0.0:0 LISTENING -1').Legal) 'a NEGATIVE PID is NOT legal'
Assert-True ((Test-D4NetstatRowFacts -Row 'TCP [::1]:1234 [::]:0 ESTABLISHED 9').Legal) 'the loopback ::1 IPv6 with a normal PID is legal'
$rowZero = Test-D4NetstatRowFacts -Row 'TCP [::]:9100 [::]:0 LISTENING 0'
Assert-True ($rowZero.Legal) 'PID 0 stays legal (netstat semantics); the :: IPv6 with 9100 LISTENING is legal'
Assert-True ($rowZero.Port9100Listening) 'the :: IPv6 9100 LISTENING row is the experiment listener'
Assert-True ((Test-D4NetstatRowFacts -Row 'TCP [::ffff:127.0.0.1]:1234 [::]:0 ESTABLISHED 7').Legal) 'an IPv4-MAPPED IPv6 address parses as IPv6 and is legal'
Assert-True ((Test-D4NetstatRowFacts -Row 'TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 0').Legal) 'a normal PID 0 on IPv4 is legal'
# one illegal IPv6 row through the EXISTING entry path -> zero reset/enable
$d126NetBadIpv6 = { param([string[]]$argv) return (New-TestRunResult -ExitCode 0 -Output @('TCP [::::]:80 [::]:0 ESTABLISHED 123')) }
$g1 = Invoke-D120T0R -Wfp $d118WfpOk -Net $d126NetBadIpv6 -ExpectThrow 'an illegal IPv6 address via the ENTRY is refused (not a legal occupancy fact)'
Assert-Equal $g1.Runner.State.ResetCount 0 'the illegal-IPv6 refusal performs ZERO resets'
Assert-Equal $g1.Runner.State.EnableCount 0 'the illegal-IPv6 refusal performs ZERO enables'


# ---- §130: REAL adapter runners + edge moved one layer out ----
Write-Host '== §130: real runners, stubbed native/dir edge (wiring fixes) =='
# The three occupancy adapters were only shape-validated before; the REAL
# environment rejected all three on first contact (netstat zh headers, pipe
# namespace, netsh duplicate-argv + in-tag CRLF). These tests run the REAL
# runner closures with a STUB only at the native-launch / directory-enum edge,
# and assert the exact argv, the exit-code ordering, stderr separation, the
# exact pipe path and the real-shape data flow through the entry.

# --- netstat runner: argv contract + exit ordering + failure ---
$r130NetSeen = @{ exe = ''; rest = @() }
$r130NetStub = {
    param([string]$Exe, [string[]]$Rest)
    $r130NetSeen['exe'] = $Exe
    $r130NetSeen['rest'] = @($Rest)
    [void](cmd /c exit 42)
    Write-Output 'TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 4321'
}.GetNewClosure()
$r130Net = New-D4RealNetStatRunner -Native $r130NetStub
[void](cmd /c exit 5)   # preset a STALE exit code - the runner must read AFTER the call
$r130NetRes = Invoke-CommandRunner -Runner $r130Net -ArgumentList @('netstat', '-ano', '-p', 'TCP')
Assert-Equal ([string]$r130NetSeen['exe']) 'netstat' 'the real netstat runner launches argv[0] as the executable'
Assert-Equal (@($r130NetSeen['rest']) -join ',') '-ano,-p,TCP' 'the real netstat runner passes the rest of the argv verbatim'
Assert-Equal $r130NetRes.ExitCode 42 'a stale preset LASTEXITCODE cannot affect the netstat runner return (exit read AFTER the call)'
Assert-True ([string]::Join(' ', @($r130NetRes.Output)) -match 'TCP 0\.0\.0\.0:1234') 'the real netstat runner returns the stdout rows'
$r130NetFail = New-D4RealNetStatRunner -Native { param($Exe, $Rest) $ErrorActionPreference = 'Continue'; [void](cmd /c exit 1); Write-Error 'native failure'; Write-Output 'partial' }
$r130NetFailRes = Invoke-CommandRunner -Runner $r130NetFail -ArgumentList @('netstat', '-ano', '-p', 'TCP')
Assert-Equal $r130NetFailRes.ExitCode 1 'a native failure from the netstat runner propagates the real exit code'
Assert-True ([string]::Join(' ', @($r130NetFailRes.Output)) -match 'native failure') 'a native stderr line stays a captured record (2>&1 merge is the netstat contract)'

# --- pipe runner: exact namespace + passthrough + failure ---
$r130PipeSeen = @{ path = '' }
$r130PipeStub = {
    param([string]$Path)
    $r130PipeSeen['path'] = $Path
    return @('\\?\pipe\um-share-broker-exp', '\\?\pipe\other')
}.GetNewClosure()
$r130Pipe = New-D4RealPipeEvidenceRunner -EnumEdge $r130PipeStub
$r130PipeRes = @(& $r130Pipe)
Assert-Equal ([string]$r130PipeSeen['path']) '\\.\pipe\' 'the real pipe runner enumerates the EXACT Win32 namespace \\.\pipe\ (character-for-character)'
Assert-Equal (@($r130PipeRes).Count) 2 'the real pipe runner passes the enumeration through unmodified'
Assert-True (@($r130PipeRes | Where-Object { $_ -match '(?i)um-share-(broker|instance)' }).Count -eq 1) 'the um-share pipe names survive the real runner'
$r130PipeThrow = New-D4RealPipeEvidenceRunner -EnumEdge { param([string]$Path) throw ('probe boom: ' + $Path) }
$r130PipeThrew = $false
try { [void](& $r130PipeThrow) } catch { $r130PipeThrew = $true }
Assert-True $r130PipeThrew 'a throwing pipe enumeration edge propagates (the caller keeps UNCONFIRMED, never a silent pass)'

# --- WFP runner: no duplicate command, exit ordering, stderr separation ---
$r130WfpSeen = @{ exe = ''; rest = @() }
$r130WfpStub = {
    param([string]$Exe, [string[]]$Rest, [string]$DumpFile)
    $ErrorActionPreference = 'Continue'
    $r130WfpSeen['exe'] = $Exe
    $r130WfpSeen['rest'] = @($Rest)
    [void](cmd /c exit 0)
    [IO.File]::WriteAllLines($DumpFile, @('<?xml version="1.0" encoding="UTF-8"?>', '<wfpdiag><filters/></wfpdiag>'), (New-Object System.Text.UTF8Encoding($true)))
    Write-Error 'ACCESS DENIED MUST NOT BECOME XML'
}.GetNewClosure()
$r130Wfp = New-D4RealWfpEvidenceRunner -Native $r130WfpStub
[void](cmd /c exit 9)
$r130WfpRes = Invoke-CommandRunner -Runner $r130Wfp -ArgumentList @('netsh', 'wfp', 'show', 'filters', 'file=-')
Assert-Equal ([string]$r130WfpSeen['exe']) 'netsh' 'the real WFP runner launches argv[0] as the executable'
Assert-Equal (@($r130WfpSeen['rest']) -join ',') 'wfp,show,filters,file=-' 'the real WFP runner NEVER re-invokes netsh as a sub-command (no duplicate)'
Assert-Equal $r130WfpRes.ExitCode 0 'a stale preset LASTEXITCODE cannot affect the WFP runner return (exit read AFTER the call)'
Assert-True ([string]::Join(' ', @($r130WfpRes.Output)) -match '<wfpdiag>') 'the real WFP runner returns the stdout XML'
Assert-True ([string]::Join(' ', @($r130WfpRes.Output)) -notmatch 'ACCESS DENIED') 'stderr NEVER masquerades as XML in the WFP runner output'
# §143 regression: the DEFAULT native edge of New-D4RealNetStatRunner must
# keep a native stderr line as a captured record even when the CALLER's
# ErrorActionPreference is Stop (the real entry is Stop by contract) - the
# REAL schtasks /Query of an ABSENT task wrote "cannot find/找不到" to stderr
# and previously threw a TERMINATING NativeCommandError, aborting T0R.
$r143Runner = New-D4RealNetStatRunner   # REAL default edge, no stub
$r143Threw = $false; $r143Res = $null
try { $r143Res = Invoke-CommandRunner -Runner $r143Runner -ArgumentList @('cmd.exe', '/c', 'echo R143-BOOM 1>&2') } catch { $r143Threw = $true }
Assert-True (-not $r143Threw) 'the DEFAULT real runner captures native stderr under a Stop caller (no terminating NativeCommandError)'
Assert-True (([string]::Join('|', @($r143Res.Output))) -match 'R143-BOOM') 'the native stderr text is captured as evidence, not lost'
Assert-Equal $r143Res.ExitCode 0 'a stderr-writing native call keeps its real exit code through the DEFAULT runner'
# the same runner against schtasks (absence is the legal-empty case)
$r143SchThrew = $false; $r143Sch = $null
try { $r143Sch = Invoke-CommandRunner -Runner $r143Runner -ArgumentList @('schtasks', '/Query', '/TN', 'd4-no-such-task-r143-xyz') } catch { $r143SchThrew = $true }
Assert-True (-not $r143SchThrew) 'schtasks /Query of an ABSENT task returns a record (never a terminating throw)'
Assert-Equal $r143Sch.ExitCode 1 'the schtasks absence query keeps its real exit code 1'
Assert-True (([string]::Join('|', @($r143Sch.Output))) -match '找不到|cannot find') 'the schtasks absence evidence text is captured for the absence classification'
# WFP + real artifact (CRLF inside <serviceName>): the runner normalization
# closes the loop for Get-D4WfpRuleEvidence on the REAL dump shape.
$r130WfpArtifact = {
    param([string]$Exe, [string[]]$Rest, [string]$DumpFile)
    # The runner routes 'file=-' through a temp file (UTF-8 BOM preserved);
    # the stub Native writes that file with the artifact XML.
    $lines = @(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<wfpdiag>'
        '  <filters>'
        '    <item>'
        '      <filterKey>{935b7f48-0ede-44dd-9bc2-e00bb635cda3}</filterKey>'
        '      <providerData/>'
        '      <se'
        'rviceName>mpssvc</serviceName>'
        '    </item>'
        '  </filters>'
        '</wfpdiag>'
    )
    [IO.File]::WriteAllLines($DumpFile, $lines, (New-Object System.Text.UTF8Encoding($true)))
}
$r130WfpArt = New-D4RealWfpEvidenceRunner -Native $r130WfpArtifact
$r130WfpVerdict = Get-D4WfpRuleEvidence -Runner $r130WfpArt -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
Assert-True (-not $r130WfpVerdict.Unconfirmed) 'the real WFP runner + normalizer turns the artifact dump into a parsed occupancy verdict'
Assert-Equal (@($r130WfpVerdict.Absent).Count) 1 'the artifact-normalized dump still proves the rule ABSENT'

# --- ConvertTo-D4WfpDumpText (normalizer) unit rules ---
# §132: the normalizer repairs ONLY the documented <serviceName> start-tag
# split (`<se<CRLF>rviceName>`, the REAL netsh MPSSVC record). Context gate =
# inside an OPEN tag NAME (no whitespace/attribute yet), both fragments
# non-empty, joined name EXACTLY "serviceName", at most ONE repair per
# document. Every other shape stays byte-for-byte: other tag-name splits
# (filterKey/filters/item), text/quote/PI and attribute-area line breaks. A
# kept split fails [xml] => UNCONFIRMED; a '<!' construct (comment/CDATA/
# DOCTYPE, verified absent from the real dump) is explicitly REJECTED with a
# never-parseable NUL sentinel.
$r132Norm1 = ConvertTo-D4WfpDumpText -Lines @('<se', 'rviceName>mpssvc</serviceName>')
Assert-Equal ([string]::Join("`n", @($r132Norm1))) '<serviceName>mpssvc</serviceName>' 'the documented in-tag-NAME <serviceName> CRLF is joined WITHOUT a space'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 1 'the <serviceName> repair is counted exactly once'
Assert-True ([UmD4WfpDumpNormalizer]::RepairReason -match 'serviceName') 'the repair reason names the repaired tag (never the rule content)'

$r132Norm2 = ConvertTo-D4WfpDumpText -Lines @('<flags numItems="3"', 'foo="1">')
Assert-Equal ([string]::Join("`n", @($r132Norm2))) "<flags numItems=`"3`"`nfoo=`"1`">" 'an attribute-area CRLF stays VERBATIM (never rewritten to a space)'
Assert-Equal (@($r132Norm2).Count) 2 'the attribute-area break keeps its own line'

$r132Norm3 = ConvertTo-D4WfpDumpText -Lines @('<root>', '<a>hello', 'world</a>', '</root>')
Assert-Equal (@($r132Norm3).Count) 4 'a CRLF in TEXT content keeps the line structure (no collapse)'
Assert-Equal ([string]$r132Norm3[1]) '<a>hello' 'the TEXT-content line break keeps its own line (never touched)'
Assert-Equal ([string]$r132Norm3[2]) 'world</a>' 'the TEXT-content line break stays inside the element text'

$r132Norm4 = ConvertTo-D4WfpDumpText -Lines @('<flags numItems="', '3">')
Assert-Equal (@($r132Norm4).Count) 2 'a CRLF inside a QUOTED attribute value keeps the line structure (never touched)'
Assert-Equal ([string]$r132Norm4[0]) '<flags numItems="' 'the quoted attribute value keeps its embedded line break (quote integrity)'

$r132Norm5 = ConvertTo-D4WfpDumpText -Lines @($([char]0xFEFF) + '<?xml version="1.0"?><wfpdiag/>')
Assert-True (([string]::Join('', @($r132Norm5))) -match '^<\?xml') 'a leading U+FEFF BOM char is stripped before the parse'
Assert-True ([UmD4WfpDumpNormalizer]::BoMStripped) 'the BOM-strip fact is recorded in the diagnostics'

$r132Norm6 = ConvertTo-D4WfpDumpText -Lines @('<a', ' b="1">')
Assert-Equal ([string]::Join("`n", @($r132Norm6))) "<a`n b=`"1`">" 'a valid-format multiline start tag is LEFT UNCHANGED (parse-equivalent without rewriting)'
$r132Norm6Doc = $null
try { $r132Norm6Doc = [xml]'<root><a
 b="1">x</a></root>' } catch { $r132Norm6Doc = $null }
Assert-True ($null -ne $r132Norm6Doc -and $null -ne $r132Norm6Doc.DocumentElement) 'a kept post-name line break still parses (legal XML S between name and attributes)'

# other tag-NAME splits are NEVER repaired: '<filterKey>' stays broken and
# unparseable (UNCONFIRMED path); a second <serviceName> split beyond the
# one-per-document limit keeps the document unparseable (rejected as-is)
$r132Norm7 = ConvertTo-D4WfpDumpText -Lines @('<filter', 'Key>dead</filterKey>')
Assert-Equal ([string]::Join("`n", @($r132Norm7))) "<filter`nKey>dead</filterKey>" 'a <filterKey> tag-name split is preserved VERBATIM (never rewritten into a legal tag)'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 0 'the <filterKey> split causes ZERO repairs'
$r132Norm7Bad = $false
try { [void][xml]([string]::Join("`n", @($r132Norm7))) } catch { $r132Norm7Bad = $true }
Assert-True $r132Norm7Bad 'a kept <filterKey> split fails [xml] so the parser reports UNCONFIRMED'

$r132Norm8 = ConvertTo-D4WfpDumpText -Lines @('<se', 'rviceName>a</serviceName>', '<se', 'rviceName>b</serviceName>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 1 'a SECOND <serviceName> split beyond the one-per-document limit is NOT repaired'
$r132Norm8Bad = $false
try { [void][xml]([string]::Join("`n", @($r132Norm8))) } catch { $r132Norm8Bad = $true }
Assert-True $r132Norm8Bad 'an over-limit second split keeps the document unparseable (rejected as-is)'

# '<!' constructs are explicitly REJECTED (the captured dump has none): a
# NUL sentinel makes the output unparseable, so Get-D4WfpRuleEvidence lands
# UNCONFIRMED instead of silently accepting an unexpected shape
$r132Norm9 = ConvertTo-D4WfpDumpText -Lines @('<wfpdiag>', '<!-- comment -->', '</wfpdiag>')
Assert-True ([UmD4WfpDumpNormalizer]::Rejected) 'an XML COMMENT is explicitly REJECTED (fail closed)'
Assert-True ([string]::Join('', @($r132Norm9)).Contains([string][char]0)) 'a rejected construct embeds a never-parseable NUL sentinel'
$r132Norm9Bad = $false
try { [void][xml]([string]::Join('', @($r132Norm9))) } catch { $r132Norm9Bad = $true }
Assert-True $r132Norm9Bad 'a comment-rejected dump fails [xml] (UNCONFIRMED path)'

$r132Norm10 = ConvertTo-D4WfpDumpText -Lines @('<wfpdiag>', '<![CDATA[x]]>', '</wfpdiag>')
Assert-True ([UmD4WfpDumpNormalizer]::Rejected) 'a CDATA section is explicitly REJECTED'
$r132Norm11 = ConvertTo-D4WfpDumpText -Lines @('<!DOCTYPE wfpdiag>', '<wfpdiag/>')
Assert-True ([UmD4WfpDumpNormalizer]::Rejected) 'a DOCTYPE declaration is explicitly REJECTED'

# a normal valid document passes through byte-for-byte with zero repairs and
# zero rejections; the diagnostics summary carries facts, never rule content
$r132Norm12Lines = @('<?xml version="1.0"?>', '<wfpdiag>', '  <filters>', '  </filters>', '</wfpdiag>')
$r132Norm12 = ConvertTo-D4WfpDumpText -Lines $r132Norm12Lines
Assert-Equal ([string]::Join("`n", @($r132Norm12))) ([string]::Join("`n", $r132Norm12Lines)) 'a normal valid document passes through byte-for-byte'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 0 'normal XML causes ZERO repairs'
Assert-True (-not [UmD4WfpDumpNormalizer]::Rejected) 'normal XML is NOT rejected'
Assert-True ([UmD4WfpDumpNormalizer]::Summary -match 'chars=') 'the dump summary carries original size facts'
Assert-True ([UmD4WfpDumpNormalizer]::Summary -notmatch '<wfpdiag') 'the dump summary never echoes raw rule text'

# §136/§137: the SECOND documented netsh wrap - a line break BETWEEN '<' and
# the tag name (`<<CRLF>flags/>`, one occurrence in the 2026-09-11-18:33
# live dump). R137: repaired ONLY for the EXACT self-closing `flags/>`
# shape - empty name accumulator, fragment EXACTLY "flags", `/>` immediately
# after. Any other name, a case variant, an attribute start, a plain open
# form, or a second occurrence stays byte-for-byte -> [xml] fails
# (UNCONFIRMED); a damaged <filters>/<filterKey> must never be fabricated
# into a legal-looking structure.
$r136Norm13 = ConvertTo-D4WfpDumpText -Lines @('<', 'flags/>')
Assert-Equal ([string]::Join("`n", @($r136Norm13))) '<flags/>' 'the documented pre-name "<"+CRLF+"flags/>" self-closing wrap is joined'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 1 'the pre-name repair is counted under RepairTagOpenCount'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 1 'the pre-name repair counts toward the total RepairCount'

$r136Norm14 = ConvertTo-D4WfpDumpText -Lines @('<', 'flags numItems="3">', '</flags>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a pre-name wrap with an ATTRIBUTE start after "flags" is NOT repaired'
Assert-Equal (@($r136Norm14).Count) 3 'the attribute pre-name break keeps every line verbatim'
$r136Norm14Bad = $false
try { [void][xml]([string]::Join("`n", @($r136Norm14))) } catch { $r136Norm14Bad = $true }
Assert-True $r136Norm14Bad 'a kept attribute pre-name break fails [xml] (UNCONFIRMED)'

$r136Norm15 = ConvertTo-D4WfpDumpText -Lines @('<', '!-- comment-wrapped -->')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a "<"+CRLF+"!--..." fragment is NOT a tag-name wrap (fragment starts with "!")'
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairCount) 0 'a non-flags pre-name fragment causes ZERO repairs'
$r136Norm15Bad = $false
try { [void][xml]([string]::Join("`n", @($r136Norm15))) } catch { $r136Norm15Bad = $true }
Assert-True $r136Norm15Bad 'a kept "<"+CRLF+"!--..." break fails [xml] (UNCONFIRMED, never fabricated into a comment)'

$r136Norm16 = ConvertTo-D4WfpDumpText -Lines @('<', 'flags/>', '<', 'flags/>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 1 'a SECOND pre-name wrap beyond the one-per-document limit is NOT repaired'
$r136Norm16Bad = $false
try { [void][xml]([string]::Join("`n", @($r136Norm16))) } catch { $r136Norm16Bad = $true }
Assert-True $r136Norm16Bad 'an over-limit second pre-name wrap keeps the document unparseable (rejected as-is)'

# §137 unit negatives: only EXACT "flags" + self-closing is repaired - every
# other pre-name break stays verbatim and unparseable
$r137Neg1 = ConvertTo-D4WfpDumpText -Lines @('<', 'filters/>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a pre-name "<"+CRLF+"filters/>" break is NEVER repaired (critical tag cannot be fabricated)'
$r137Neg1Bad = $false
try { [void][xml]([string]::Join("`n", @($r137Neg1))) } catch { $r137Neg1Bad = $true }
Assert-True $r137Neg1Bad 'a kept "<"+CRLF+"filters/>" break fails [xml]'

$r137Neg2 = ConvertTo-D4WfpDumpText -Lines @('<', 'Flags/>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a case-variant pre-name wrap is NOT repaired (exact ordinal "flags")'

$r137Neg3 = ConvertTo-D4WfpDumpText -Lines @('<', 'flags>')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a pre-name wrap on a PLAIN (non-self-closing) open tag is NOT repaired'

$r137Neg4 = ConvertTo-D4WfpDumpText -Lines @('<', 'flags />')
Assert-Equal ([UmD4WfpDumpNormalizer]::RepairTagOpenCount) 0 'a pre-name wrap with a SPACE before "/>" is NOT repaired (attribute area)'

# --- §132 parser-level: the narrowed normalizer at the Get-D4WfpRuleEvidence
# boundary (PRESENT not lost for the repaired split; UNCONFIRMED for
# unverified shapes) ---
$r132PresentArt = {
    param($Exe, $Rest, $DumpFile)
    $out = @(
        '<?xml version="1.0" encoding="UTF-8"?>', '<wfpdiag>', '  <filters>', '    <item>',
        '      <filterKey>{a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8}</filterKey>',
        '      <se', 'rviceName>mpssvc</serviceName>', '    </item>', '  </filters>', '</wfpdiag>'
    )
    [IO.File]::WriteAllLines($DumpFile, $out, (New-Object System.Text.UTF8Encoding($true)))
}
$r132Present = New-D4RealWfpEvidenceRunner -Native $r132PresentArt
$r132PresentVerdict = Get-D4WfpRuleEvidence -Runner $r132Present -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
Assert-True (-not $r132PresentVerdict.Unconfirmed) 'the documented <serviceName> split still yields a parsed occupancy verdict (PRESENT not lost)'
Assert-Equal (@($r132PresentVerdict.Present).Count) 1 'the repaired dump still proves the target rule PRESENT'

$r132BadFkArt = {
    param($Exe, $Rest, $DumpFile)
    $out = @(
        '<?xml version="1.0" encoding="UTF-8"?>', '<wfpdiag>', '  <filters>', '    <item>',
        '      <filter', 'Key>{935b7f48-0ede-44dd-9bc2-e00bb635cda3}</filterKey>',
        '    </item>', '  </filters>', '</wfpdiag>'
    )
    [IO.File]::WriteAllLines($DumpFile, $out, (New-Object System.Text.UTF8Encoding($true)))
}
$r132BadFk = New-D4RealWfpEvidenceRunner -Native $r132BadFkArt
$r132BadFkVerdict = Get-D4WfpRuleEvidence -Runner $r132BadFk -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
Assert-True $r132BadFkVerdict.Unconfirmed 'a <filterKey> tag-name split is NEVER normalized into a legal tag: UNCONFIRMED'

$r132CommentArt = {
    param($Exe, $Rest, $DumpFile)
    $out = @(
        '<?xml version="1.0" encoding="UTF-8"?>', '<wfpdiag>',
        '  <!-- unexpected comment -->', '  <filters>', '  </filters>', '</wfpdiag>'
    )
    [IO.File]::WriteAllLines($DumpFile, $out, (New-Object System.Text.UTF8Encoding($true)))
}
$r132Comment = New-D4RealWfpEvidenceRunner -Native $r132CommentArt
$r132CommentVerdict = Get-D4WfpRuleEvidence -Runner $r132Comment -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
Assert-True $r132CommentVerdict.Unconfirmed 'an unverified COMMENT in the dump is explicitly rejected: UNCONFIRMED (never accepted as a normal document)'

# §136 parser-level: a dump carrying BOTH documented netsh wraps (the
# in-name <serviceName> split AND the pre-name '<'+CRLF tag-open wrap)
# parses with BOTH repaired and the target PRESENT is not lost.
$r136BothArt = {
    param($Exe, $Rest, $DumpFile)
    $out = @(
        '<?xml version="1.0" encoding="UTF-8"?>', '<wfpdiag>', '  <filters>', '    <item>',
        '      <filterKey>{a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8}</filterKey>',
        '      <se', 'rviceName>mpssvc</serviceName>', '    </item>', '  </filters>',
        '  <providers>', '    <item>',
        '      <providerKey>FWPM_PROVIDER_MPSSVC_APP_ISOLATION</providerKey>',
        '      <', 'flags/>', '    </item>', '  </providers>', '</wfpdiag>'
    )
    [IO.File]::WriteAllLines($DumpFile, $out, (New-Object System.Text.UTF8Encoding($true)))
}
$r136Both = New-D4RealWfpEvidenceRunner -Native $r136BothArt
$r136BothVerdict = Get-D4WfpRuleEvidence -Runner $r136Both -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
Assert-True (-not $r136BothVerdict.Unconfirmed) 'a dump carrying BOTH documented wraps parses with both repaired (PRESENT not lost)'
Assert-Equal (@($r136BothVerdict.Present).Count) 1 'the both-repaired dump still proves the target rule PRESENT'

# §137 parser-level: pre-name breaks on CRITICAL structure tags are NEVER
# repaired by Get-D4WfpRuleEvidence - a damaged <filters>/<filterKey>/<item>/
# <wfpdiag> stays broken -> UNCONFIRMED (an empty-filters ABSENT-looking
# verdict can never be synthesized from a damaged structure). A flags WITH
# attribute also stays broken.
$r137BreakCases = @(
    @{ name = 'filters'; lines = @('<wfpdiag>', '<', 'filters/>', '</wfpdiag>') },
    @{ name = 'item'; lines = @('<wfpdiag>', '<filters>', '<', 'item>', '</filters>', '</wfpdiag>') },
    @{ name = 'filterKey'; lines = @('<wfpdiag>', '<filters>', '<item>', '<', 'filterKey>{a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8}</filterKey>', '</item>', '</filters>', '</wfpdiag>') },
    @{ name = 'wfpdiag'; lines = @('<', 'wfpdiag>', '</wfpdiag>') },
    @{ name = 'flags-with-attr'; lines = @('<wfpdiag>', '<filters>', '<item>', '<filterKey>{a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8}</filterKey>', '<', 'flags numItems="2">', '</item>', '</filters>', '</wfpdiag>') }
)
foreach ($kc in $r137BreakCases) {
    $kcRunner = New-D4RealWfpEvidenceRunner -Native {
        param($Exe, $Rest, $DumpFile)
        [IO.File]::WriteAllLines($DumpFile, $kc.lines, (New-Object System.Text.UTF8Encoding($true)))
    }
    $kcVerdict = Get-D4WfpRuleEvidence -Runner $kcRunner -RuleIds @('a204e991-cae6-42fd-bbcd-bbe2ef3ba2e8')
    Assert-True $kcVerdict.Unconfirmed ('a pre-name <' + $kc.name + '> break is NEVER repaired: UNCONFIRMED (critical structure cannot be fabricated)')
}

# --- ENTRY-level integration: the REAL runners drive the REAL probe ---
$r130OkBase = New-D118Base
$r130OkRun = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
# real-shape netstat rows: LOCALIZED zh title/column headers + one legal row
$r130NetReal = New-D4RealNetStatRunner -Native {
    param($Exe, $Rest)
    [void](cmd /c exit 0)
    Write-Output ''
    Write-Output '活动连接'
    Write-Output ''
    Write-Output '  协议  本地地址          外部地址        状态           PID'
    Write-Output '  TCP    0.0.0.0:1234     0.0.0.0:0      LISTENING      4321'
}
$r130WfpArtFull = New-D4RealWfpEvidenceRunner -Native $r130WfpArtifact
$r130PipeOk = New-D4RealPipeEvidenceRunner -EnumEdge { param([string]$Path) return @('\\?\pipe\unrelated-pipe') }
$r130OKSplat = New-D118Splat -Os $r130OkBase.Os -PlanPath $r130OkBase.PlanPath -Emit $r130OkBase.Emit -Runner $r130OkRun `
    -Net $r130NetReal -Proc $d118ProcEmpty -Wfp $r130WfpArtFull -Pipe $r130PipeOk -Schtasks $r130OkBase.Sch
$r130OkThrew = $false; $r130OkErr = ''
try { & $entry -Stage 'T0R' @r130OKSplat | Out-Null; $r130OkThrew = ($LASTEXITCODE -ne 0) } catch { $r130OkThrew = $true; $r130OkErr = $_.Exception.Message }
Assert-True (-not $r130OkThrew) ('the REAL three-runner occupancy (zh headers + artifact dump + clean pipes) lets the recovery proceed: ' + [string]$r130OkErr)
Assert-Equal $r130OkRun.State.ResetCount 1 'the real-runner positive recovery resets EXACTLY once'
# negative control: a PIPE enumeration FAILURE with the SAME other edges
$r130PipeBad = New-D4RealPipeEvidenceRunner -EnumEdge { param([string]$Path) throw 'enum failed' }
$r130PipeBase = New-D118Base
$r130PipeRun = New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed
$r130PipeSplat = New-D118Splat -Os $r130PipeBase.Os -PlanPath $r130PipeBase.PlanPath -Emit $r130PipeBase.Emit -Runner $r130PipeRun `
    -Net $r130NetReal -Proc $d118ProcEmpty -Wfp $r130WfpArtFull -Pipe $r130PipeBad -Schtasks $r130PipeBase.Sch
$r130PipeThrew2 = $false
try { & $entry -Stage 'T0R' @r130PipeSplat | Out-Null; $r130PipeThrew2 = ($LASTEXITCODE -ne 0) } catch { $r130PipeThrew2 = $true }
Assert-True $r130PipeThrew2 'a throwing real pipe edge refuses the ENTRY recovery (UNCONFIRMED, not clean)'
Assert-Equal $r130PipeRun.State.ResetCount 0 'the pipe-failure refusal performs ZERO resets'

# --- Close driver no-record fixture: the REAL boundary returns the CLEAN reason ---
$r130CloseOs = New-D4InMemoryOs -Root $contract['ExperimentRoot'] -ExtraRoots @('C:\ProgramData\UMShareExp-bootstrap')
$r130ClosePath = Join-Path 'C:\ProgramData\UMShareExp-bootstrap' 'bootstrap-recovery-norec.json'
$r130CloseRes = Invoke-D4RecoveryClose -Contract $contract -RunId 'd4x-norec' `
    -AccountName 'um_share_d4' -TaskName $contract['ShareTaskName'] `
    -FixedSid $d115Fixed -RecoveryStatePath $r130ClosePath `
    -PackageHash '' -PlanIdentity '' `
    -AccountRunner (New-D4FakeRecoveryRunner -TaskModel @{} -FixedSid $d115Fixed) `
    -FileBoundary $r130CloseOs.Boundary `
    -StoreProbe { param([string]$Path) return $null } -AclProbe (New-D4RealAclProbe -GetAclEdge $d115AclTrusted)
Assert-True (-not [bool]$r130CloseRes.ok) 'the close driver for an ABSENT recovery record does not claim success'
Assert-True ([string]$r130CloseRes.reason -match 'no recovery record exists') ('the close driver reports the clean absent-record reason: ' + [string]$r130CloseRes.reason)
Assert-True ([string]$r130CloseRes.reason -notmatch 'Null|unreadable') 'the absent-record close reason is NEVER the null-method unreadable crash'


if ($script:failures -gt 0) {
    Write-Host ("d4-assembly offline tests: {0} FAILURES" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'd4-assembly offline tests: ALL PASS' -ForegroundColor Green
exit 0
