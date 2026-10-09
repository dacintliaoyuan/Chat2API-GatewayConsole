# install-share-broker-lib.ps1 - function library for the SYSTEM broker host
# install/uninstall (D4 prerequisite). Dot-sourced by install-share-broker.ps1
# and by the offline tests (tests/install-share-broker.Tests.ps1); contains NO
# top-level execution, so loading it never touches the real host.
#
# R41-5/R41-6 contracts implemented here:
# * Manifest classification BEFORE any copy: a pre-existing destination file
#   with the SAME hash is "shared" (kept; never recorded as owned), with a
#   DIFFERENT hash is a conflict that REFUSES the install (the broker install
#   never overwrites the share deployment's own copies), absent -> "owned".
# * Copy-VerifiedModule returns EXACTLY one value (the SHA256 string); its
#   log lines go to the host stream (Write-Host), never into the pipeline.
# * The broker dir protection boundary (SYSTEM/admin full, the ordinary
#   operator account minimal RX, um_share NO access) is verified by parsing
#   icacls output BEFORE the control secret is written; a failed icacls exit
#   code or a failed verification refuses the install.
# * Uninstall confirms the task is stopped AND deleted before it removes any
#   file, deletes ONLY manifest-owned entries through a pure-filename
#   whitelist resolved inside the target root, and never blind-recurses a
#   directory that still holds unknown content.
# * The run-broker.cmd wrapper is GENERATED from the install parameters and
#   derives every runtime path from its own directory (%~dp0), so an
#   experiment install under a different root points exclusively at the
#   experiment namespace.

$script:MANAGED_FILE_PATTERN = '^[A-Za-z0-9][A-Za-z0-9_.\-]*$'

function Write-Log {
    param([string]$msg)
    # Host stream on purpose: log lines must never flow into a return value.
    Write-Host ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
}

function Test-ManagedFileName {
    param([string]$Name)
    return ($Name -match $script:MANAGED_FILE_PATTERN)
}

function Test-ReparseFree {
    # R44-6: a junction/reparse point INSIDE the target root can redirect
    # deletions outside the root. Every relative component (and the final
    # entry, when it exists) must be a regular object; the root itself is
    # the operator-chosen root and is not re-checked.
    param([string]$TargetRoot, [string]$RelPath)
    $acc = $TargetRoot.TrimEnd('\')
    foreach ($p in @($RelPath -split '\\')) {
        $acc = Join-Path $acc $p
        if (Test-Path -LiteralPath $acc) {
            $item = Get-Item -LiteralPath $acc -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw ("reparse point inside the target root refused: " + $RelPath)
            }
        }
    }
}

function Get-ManagedPath {
    # Resolve a manifest RELATIVE path STRICTLY inside the target root:
    # every path segment must pass the pure-filename whitelist (no
    # separators inside a segment, no colons, no traversal dots), the
    # resolved location must stay under the target root, and every
    # intermediate component must be reparse-free (R44-6).
    param([string]$TargetRoot, [string]$RelPath)
    if (-not $RelPath) { throw 'manifest rel path is empty' }
    $parts = @($RelPath -split '\\')
    foreach ($p in $parts) {
        if (-not (Test-ManagedFileName $p)) {
            throw ("manifest path segment rejected (whitelist): " + $RelPath)
        }
    }
    $resolved = $TargetRoot
    foreach ($p in $parts) { $resolved = Join-Path $resolved $p }
    $rootPrefix = $TargetRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw ("manifest path escaped the target root: " + $RelPath)
    }
    Test-ReparseFree -TargetRoot $TargetRoot -RelPath $RelPath
    return $resolved
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function New-TestRunResult {
    param([int]$ExitCode, [object[]]$Output)
    [pscustomobject]@{ ExitCode = $ExitCode; Output = @($Output) }
}

function Invoke-CommandRunner {
    # Runs the injected schtasks/icacls runner (or the real command) and
    # ALWAYS returns a structured result; a missing executable is an
    # explicit failure, never a silent pass.
    param([scriptblock]$Runner, [string[]]$ArgumentList)
    if ($Runner) {
        return & $Runner $ArgumentList
    }
    $out = & $ArgumentList[0] @($ArgumentList | Select-Object -Skip 1) 2>&1
    return (New-TestRunResult -ExitCode $LASTEXITCODE -Output @($out))
}

function Get-BrokerSourceManifest {
    # Module closure copied flat into the target root (D0 27.4). The whole
    # closure is reviewed code; Dst values are PURE file names.
    param([string]$SourceRoot)
    return @(
        @{ Src = (Join-Path $SourceRoot 'share_broker_protocol.py');  File = 'share_broker_protocol.py' },
        @{ Src = (Join-Path $SourceRoot 'share_broker_host.py');      File = 'share_broker_host.py' },
        @{ Src = (Join-Path $SourceRoot 'share_broker_client.py');    File = 'share_broker_client.py' },
        @{ Src = (Join-Path $SourceRoot 'share_broker_ops.py');       File = 'share_broker_ops.py' },
        @{ Src = (Join-Path $SourceRoot 'share_broker_server.py');    File = 'share_broker_server.py' },
        @{ Src = (Join-Path $SourceRoot 'share_instance_pipe.py');    File = 'share_instance_pipe.py' },
        @{ Src = (Join-Path $SourceRoot 'share_runtime.py');          File = 'share_runtime.py' },
        @{ Src = (Join-Path $SourceRoot 'share_management.py');       File = 'share_management.py' },
        @{ Src = (Join-Path $SourceRoot 'share_store.py');            File = 'share_store.py' }
    )
}

function Copy-VerifiedModule {
    # R41-6 fix: the log line goes to the HOST stream, so the return value
    # is exactly ONE string (the SHA256) and the install manifest's sha256
    # field can never become an array of log lines.
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw ("broker source module missing: " + $Source)
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    $srcHash = Get-FileSha256 $Source
    $dstHash = Get-FileSha256 $Destination
    if ($srcHash -ne $dstHash) { throw ("run copy hash mismatch for " + $Destination) }
    Write-Log ("copied+verified " + (Split-Path $Destination -Leaf) + " (" + $srcHash.Substring(0, 12) + "...)")
    return $srcHash
}

function New-BrokerSecret {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $rng.Dispose()
    ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Write-BrokerKeyFile {
    param([string]$Path, [string]$Label)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $existing = ([IO.File]::ReadAllText($Path)).Trim()
        if ($existing -notmatch '^[0-9a-f]{64}$') {
            throw ($Label + " exists but is not a 64-hex value: " + $Path)
        }
        Write-Log ($Label + " already present (kept)")
        return $false
    }
    $secret = New-BrokerSecret
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($tmp, $secret, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
    Write-Log ($Label + " generated (" + (Split-Path $Path -Leaf) + ")")
    return $true
}

function Set-BrokerDirAcl {
    # R41-5: SYSTEM/admin full + the ordinary operator account minimal RX;
    # inheritance removed so um_share keeps NO access. The native exit code
    # is checked - a failed icacls refuses the install.
    param([string]$BrokerDir, [string]$OperatorAccount, [scriptblock]$IcaclsRunner)
    # The runner contract is FULL argv with the command name FIRST (&
    # $ArgumentList[0] ...). Every other call site passes 'icacls'
    # explicitly; this site omitted it, so the REAL runner tried to execute
    # the broker dir path itself (offline fake runners never parse argv[0],
    # which is why offline tests could not see this divergence).
    $args = @('icacls', $BrokerDir, '/inheritance:r',
        '/grant:r', 'SYSTEM:(OI)(CI)F',
        '/grant:r', 'BUILTIN\Administrators:(OI)(CI)F',
        '/grant:r', ($OperatorAccount + ':(OI)(CI)RX'))
    $result = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList $args
    if ($result.ExitCode -ne 0) {
        Write-Log ("icacls failed (exit " + $result.ExitCode + "): " + ($result.Output -join ' '))
        return $false
    }
    return $true
}

function Test-AclOutput {
    # Structured verdict over icacls OUTPUT (pure function, offline
    # testable): expected full identities hold EFFECTIVE full control, the
    # read-only identity holds ONLY read codes, the forbidden identities
    # are absent, every other principal is write-free, and every non-path
    # line parses. R44-4: evaluation is deny-override (effective = ALLOW -
    # DENY per principal, a DENY ACE can never count as a grant), only
    # KNOWN permission codes are accepted (unknown codes refuse), and the
    # echoed path header is skipped ONLY on an exact match - a line that
    # merely STARTS with the path has its remainder parsed as an ACE, so a
    # dangerous ACE behind the path prefix is never dropped.
    param(
        [string[]]$Output,
        [string]$TargetPath,         # the queried path (echo-header match)
        [string[]]$AllowedFull,      # e.g. 'SYSTEM', 'BUILTIN\Administrators'
        [string]$ReadOnlyIdentity,   # the configured 8096 operator
        [string[]]$Forbidden,        # e.g. 'um_share_public'
        # R67-B: a LIMITED-WRITE identity map (name -> allowed write codes):
        # a matching principal must hold EXACTLY the allowed read+write set
        # (any code outside the allowed set refuses) and is exempt from the
        # unexpected-principal refusal. Used for the run\live registration
        # spot (um_share: create/overwrite the registration file ONLY).
        [hashtable]$LimitedWrite
    )
    $writeCodes = @('F', 'M', 'W', 'D', 'WD', 'AD', 'WEA', 'WA', 'DC', 'DE',
                    'WDAC', 'WO', 'GW', 'GA')
    $readCodes = @('R', 'RX', 'RD', 'REA', 'RA', 'RC', 'X', 'GR', 'GE', 'S')
    $fullCodes = @('F', 'GA')
    $knownCodes = @($writeCodes) + @($readCodes)
    $flagCodes = @('I', 'OI', 'CI', 'ID', 'NP', 'OD')
    $allow = @{}
    $denied = @{}
    $seen = 0
    $targetLower = "$TargetPath".ToLowerInvariant()
    foreach ($line in $Output) {
        $trimmed = ("$line").Trim()
        if (-not $trimmed) { continue }
        $seen = 1
        $lower = $trimmed.ToLowerInvariant()
        if ($lower -eq $targetLower) {
            # icacls echoes the QUERIED path as a header line (exact match
            # only - a drive-letter colon would otherwise be misparsed).
            continue
        }
        $text = $trimmed
        if ($lower.StartsWith($targetLower)) {
            # R44-4: a line that merely STARTS with the queried path is NOT
            # silently dropped - its REMAINDER is parsed as an ACE, so a
            # dangerous ACE behind the path prefix is checked.
            $text = $trimmed.Substring($TargetPath.Length)
            if (-not $text.Trim()) { continue }
        }
        $idx = $text.LastIndexOf(':')
        if ($idx -lt 1) {
            # icacls prints a localized operation SUMMARY after every command
            # (Chinese locale: "已成功处理 N 个文件; 处理 M 个文件时失败";
            # English: "Successfully processed N files; Failed processing M
            # files") - observed on real plain queries, never an ACE. Skip
            # ONLY these exact summary shapes; any other unparsable line
            # still refuses (the strict-parse safety posture is unchanged).
            if ($text -match '^已成功处理 \d+ 个文件' -or
                    $text -match '^Successfully processed \d+ files?') { continue }
            return @{ Ok = $false; Reason = "unparsable ACL line: $text" }
        }
        $name = $text.Substring(0, $idx).Trim()
        $flagPart = $text.Substring($idx + 1)
        foreach ($forbiddenName in $Forbidden) {
            if ($name -like "*$forbiddenName*") {
                return @{ Ok = $false; Reason = "forbidden identity present: $forbiddenName" }
            }
        }
        $codes = @()
        $isDeny = $false
        foreach ($token in ($flagPart.Split('(') | ForEach-Object { $_.Trim(')', ' ', "`t") })) {
            if (-not $token) { continue }
            if ($token -eq 'SPECIAL_ACCESS:') {
                return @{ Ok = $false; Reason = "SPECIAL_ACCESS detail not supported" }
            }
            foreach ($piece in ($token -replace ' ', '').Split(',')) {
                if (-not $piece) { continue }
                if ($piece -eq 'DENY') { $isDeny = $true; continue }
                if ($piece -in $flagCodes) { continue }
                if ($piece -notin $knownCodes) {
                    return @{ Ok = $false; Reason = "unknown permission code: $piece" }
                }
                $codes += $piece
            }
        }
        if (-not $codes) {
            return @{ Ok = $false; Reason = "ACE without permission codes: $name" }
        }
        if (($name.ToLowerInvariant().StartsWith('mandatory label')) -and
                (-not @($codes | Where-Object { $_ -notin @('NW', 'NR', 'NE') }))) {
            # Microsoft icacls prints the mandatory-integrity policy on the
            # synthetic 'Mandatory Label' principal; NW/NR/NE are policy
            # levels, NOT grants or strangers - excluded from the union
            # (R46.6-C contract; real shapes are L2-calibrated).
            continue
        }
        $target = $denied
        if (-not $isDeny) { $target = $allow }
        if (-not $target.ContainsKey($name)) { $target[$name] = @() }
        $target[$name] = @($target[$name]) + $codes
    }
    if ($seen -eq 0) { return @{ Ok = $false; Reason = "icacls output empty" } }
    # R49-5: DENY carrying SPECIFIC codes cannot be interpreted in the
    # token model -> unsupported combination refuses; only aggregated
    # DENY codes (F/GA) are interpretable (clear the grant). An aggregated
    # ALLOW (F/GA) normalizes to every known code before subtraction.
    foreach ($dname in @($denied.Keys)) {
        $unsupported = @(@($denied[$dname]) | Where-Object { $_ -notin @('F', 'GA') })
        if ($unsupported.Count -gt 0) {
            return @{ Ok = $false;
                      Reason = ("unsupported deny granularity on " + $dname + ": " + ($unsupported -join ',')) }
        }
    }
    $effective = {
        param($name)
        $grantedCodes = @($allow[$name])
        if (@($grantedCodes | Where-Object { $_ -in @('F', 'GA') })) {
            $grantedCodes = $knownCodes
        }
        $denyCodes = @($denied[$name])
        if (@($denyCodes | Where-Object { $_ -in @('F', 'GA') })) {
            return @()
        }
        @($grantedCodes | Where-Object { $_ -notin $denyCodes })
    }
    $norm = {
        param([string]$IdText)
        $t = ([string]$IdText).Trim()
        if ($t.StartsWith('*', [System.StringComparison]::Ordinal)) { $t = $t.Substring(1) }
        if ([regex]::IsMatch($t, '^S-1-5-')) {
            try { $sidO = New-Object System.Security.Principal.SecurityIdentifier($t); return @{ ok = $true; sid = [string]$sidO.Value }; } catch { }
        }
        try {
            $ntO = New-Object System.Security.Principal.NTAccount($t)
            $sidT = $ntO.Translate([System.Security.Principal.SecurityIdentifier])
            return @{ ok = $true; sid = [string]$sidT.Value }
        } catch { }
        return @{ ok = $false; sid = $t.ToLowerInvariant() }
    }.GetNewClosure()
    $sameId = {
        param([string]$A, [string]$B)
        $nA = & $norm $A
        $nB = & $norm $B
        if ($nA.ok -and $nB.ok) { return ($nA.sid -ceq $nB.sid) }
        return ([string]$A -eq [string]$B -or [string]$A -like ('*\' + $B) -or [string]$B -like ('*\' + $A))
    }.GetNewClosure()
    # icacls re-exports principals as ACCOUNT NAMES (HOST\um_share_public,
    # NT AUTHORITY\SYSTEM) while expected matrix identities may be SID literals
    # ('*S-1-5-21-...-1007'). Compare identities by CANONICAL SID (real Windows
    # resolution; a SID literal resolves to itself; a name resolves via NTAccount).
    # An unresolvable text falls back to literal/suffix string matching - the
    # classification is never LOOSER than before (an unresolvable observed
    # principal that matches nothing still refuses below).

    foreach ($full in $AllowedFull) {
        $match = @($allow.Keys | Where-Object { $sameId.Invoke($full, $_) })
        if (-not $match) {
            return @{ Ok = $false; Reason = "missing full grant for $full" }
        }
        $eff = & $effective $match[0]
        if (-not ($eff | Where-Object { $_ -in $fullCodes })) {
            return @{ Ok = $false; Reason = "$full lacks full control (got: $($eff -join ','))" }
        }
    }
    if ($ReadOnlyIdentity) {
        $match = @($allow.Keys | Where-Object { $sameId.Invoke($ReadOnlyIdentity, $_) })
        if (-not $match) {
            return @{ Ok = $false; Reason = "missing read grant for $ReadOnlyIdentity" }
        }
        $eff = & $effective $match[0]
        $writeHit = @($eff | Where-Object { $_ -in $writeCodes })
        if ($writeHit) {
            return @{ Ok = $false; Reason = "$ReadOnlyIdentity holds write codes: $($writeHit -join ',')" }
        }
        if (-not ($eff | Where-Object { $_ -in $readCodes })) {
            return @{ Ok = $false; Reason = "$ReadOnlyIdentity lacks read codes (got: $($eff -join ','))" }
        }
    }
    foreach ($name in $allow.Keys) {
        $isFull = ($AllowedFull | Where-Object { $sameId.Invoke($name, $_) })
        $isRead = ($ReadOnlyIdentity -and $sameId.Invoke($name, $ReadOnlyIdentity))
        $isLimited = $false
        if ($LimitedWrite) {
            foreach ($lname in @($LimitedWrite.Keys)) {
                if ($sameId.Invoke($name, $lname)) {
                    $isLimited = $true
                    # The limited-write principal must hold EXACTLY the
                    # allowed set: every effective code must be inside the
                    # allow-list (any extra code refuses), and every allowed
                    # code must be present (a missing grant = tamper).
                    $effL = & $effective $name
                    $allowedCodes = @($LimitedWrite[$lname])
                    $extra = @($effL | Where-Object { $_ -notin $allowedCodes })
                    if ($extra.Count -gt 0) {
                        return @{ Ok = $false; Reason = "$lname holds codes outside the registration spot: $($extra -join ',')" }
                    }
                    foreach ($need in $allowedCodes) {
                        if (-not (@($effL | Where-Object { $_ -eq $need }).Count -gt 0)) {
                            return @{ Ok = $false; Reason = "$lname lacks required code: $need" }
                        }
                    }
                }
            }
        }
        if ($isFull -or $isRead -or $isLimited) { continue }
        $eff = & $effective $name
        # R46.6-C: the broker dir is a CONTROLLED namespace - ANY unlisted
        # principal refuses (reading the control secret's directory is as
        # disqualifying as writing it).
        return @{ Ok = $false; Reason = "unexpected principal ${name} holds: $($eff -join ',')" }
    }
    # R71-C: a REQUIRED limited-write identity must actually EXIST with its
    # allowed set - a principal completely absent from the query output can
    # never pass (the run\live spot without um_share would silently deny
    # the share's registration/status writes). The check starts from the
    # LimitedWrite.Keys, not from the observed principals, so a fully
    # missing identity is caught here.
    if ($LimitedWrite) {
        $seenLimited = @{}
        foreach ($name in $allow.Keys) {
            foreach ($lname in @($LimitedWrite.Keys)) {
                if ($sameId.Invoke($name, $lname)) {
                    if (-not $seenLimited.ContainsKey($lname)) { $seenLimited[$lname] = $true }
                }
            }
        }
        foreach ($lname in @($LimitedWrite.Keys)) {
            if (-not $seenLimited.ContainsKey($lname)) {
                return @{ Ok = $false; Reason = "required limited-write identity missing: $lname" }
            }
        }
    }
    return @{ Ok = $true; Reason = $null }
}

function Test-BrokerDirAcl {
    # Query the broker dir ACL and fail closed unless the protection
    # boundary verifiably holds: SYSTEM/admin full, the operator account
    # read-only, um_share absent, no stray write principals. This runs
    # BEFORE the control secret is written (R41-5).
    param([string]$BrokerDir, [string]$OperatorAccount, [scriptblock]$IcaclsRunner)
    $result = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @('icacls', $BrokerDir)
    if ($result.ExitCode -ne 0) {
        return @{ Ok = $false; Reason = ("icacls exit " + $result.ExitCode + ": " + ($result.Output -join ' ')) }
    }
    return (Test-AclOutput -Output @($result.Output) -TargetPath $BrokerDir -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity $OperatorAccount -Forbidden @('um_share_public'))
}

function New-BrokerWrapperContent {
    # Pure function: the GENERATED wrapper. Every runtime path derives from
    # the wrapper's own directory (%~dp0 = the install target root), so an
    # experiment install under a different root can never reference
    # production paths. SDDL and owner-SID calibration are optional
    # parameters; when absent the server's placeholder SDDL refuses to
    # serve (deliberate fail-closed until L2).
    param([hashtable]$Cfg)
    $sddlArg = ''
    if ($Cfg['BrokerPipeSddl']) { $sddlArg = ' --sddl "' + $Cfg['BrokerPipeSddl'] + '"' }
    # P1 (D4-P §60.2): the template ALREADY carries the option name and the
    # quotes - the replacement is the PURE VALUE. Substituting a full
    # "--expected-owner-account ..." fragment here re-nested the option and
    # the quotes whenever an account was configured.
    $ownerArg = [string]$Cfg['ExpectedOwnerAccount']
    $sidArg = ''
    if ($Cfg['InstanceOwnerSid']) { $sidArg = ' --expect-owner-sid ' + $Cfg['InstanceOwnerSid'] }
    $template = @'
@echo off
rem run-broker.cmd - SYSTEM scheduled task wrapper for the broker host.
rem GENERATED by install-share-broker.ps1 from the install parameters;
rem runtime paths derive from THIS script's directory (%~dp0), so an
rem experiment install never touches production.
rem The explicit cd pins the working directory regardless of the task
rem scheduler's WorkingDirectory setting: the packaged interpreter was
rem observed to fail its stdlib/pipe setup when launched from an unrelated
rem cwd (W4 defect @4), so launch always starts from the install root.
cd /d "%~dp0"
setlocal
set "PYTHONHOME=__PYTHONHOME__"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH="
"%~dp0um-share-python.exe" "%~dp0share_broker_server.py" --broker-dir "%~dp0broker" --key-file "%~dp0broker\broker-control-key" --instance-key-file "%~dp0instance-channel-key" --registration-file "%~dp0run\live\instance-registration.json" --pipe-name "__BROKER_PIPE__" --instance-pipe-prefix "__INSTANCE_PREFIX__" --projection-path "%~dp0um-share-projection.json" --share-task "__SHARE_TASK__" --share-port __SHARE_PORT__ --expected-owner-account "__EXPECTED_OWNER__" --expect-image "%~dp0um-share-python.exe" --watchdog-script "%~dp0watchdog-umshare.ps1" --run-root "%~dp0\" --firewall-rule "um-share-public-outbound-block" --share-faults-file "%~dp0run\live\share-faults.jsonl" --dispatch-deadline 130__SDDL____SID__ %*
endlocal
'@
    $content = $template.Replace('__PYTHONHOME__', $Cfg['PythonHome']).Replace('__BROKER_PIPE__', $Cfg['BrokerPipeName']).Replace('__INSTANCE_PREFIX__', $Cfg['InstancePipePrefix']).Replace('__SHARE_TASK__', $Cfg['ShareTaskName']).Replace('__SHARE_PORT__', [string]$Cfg['SharePort']).Replace('__EXPECTED_OWNER__', $OwnerArg).Replace('__SDDL__', $sddlArg).Replace('__SID__', $sidArg)
    return $content
}

function Get-BrokerWrapperServerArgv {
    # P1 (D4-P §60.4-1): the ARGV-LEVEL contract of the generated wrapper -
    # what um-share-python.exe will ACTUALLY receive, parsed from the
    # wrapper TEXT itself (never a parallel builder that can drift from the
    # template). %~dp0 resolves to the install target root (with its
    # trailing backslash); the operator tail %* is not part of the install
    # contract and is dropped. Tokenization follows the MSVC command-line
    # rules the child applies: spaces outside quotes split, a quoted run
    # merges into ONE token (a value with spaces or backslashes is never
    # split), 2n backslashes before a quote collapse to n + literal quote.
    param([string]$WrapperContent, [string]$TargetRoot)
    $line = @($WrapperContent -split "`r?`n") |
        Where-Object { $_ -match 'um-share-python\.exe' } |
        Select-Object -First 1
    if (-not $line) {
        throw "wrapper does not contain the broker server launch line"
    }
    $line = $line.Trim()
    if (-not $line.EndsWith('%*')) {
        throw ("unexpected wrapper launch line tail: " + $line)
    }
    $line = $line.Substring(0, $line.Length - 2).TrimEnd()
    $resolved = $line.Replace('%~dp0', $TargetRoot + '\')
    $tokens = New-Object System.Collections.Generic.List[string]
    $current = New-Object Text.StringBuilder
    $hadQuote = $false
    $inQuotes = $false
    $i = 0
    while ($i -lt $resolved.Length) {
        $c = $resolved[$i]
        if ($c -eq '\') {
            # MSVC: backslash runs are literal unless they precede a quote.
            $runs = 0
            while ($i -lt $resolved.Length -and $resolved[$i] -eq '\') {
                $runs++; $i++
            }
            if ($i -lt $resolved.Length -and $resolved[$i] -eq '"') {
                $full = [int]($runs / 2)
                for ($k = 0; $k -lt $full; $k++) { [void]$current.Append('\') }
                if ($runs % 2 -eq 0) {
                    $inQuotes = -not $inQuotes; $hadQuote = $true; $i++
                } else {
                    [void]$current.Append('"'); $i++
                }
            } else {
                for ($k = 0; $k -lt $runs; $k++) { [void]$current.Append('\') }
            }
            continue
        }
        if ($c -eq '"') {
            if ($inQuotes -and $i + 1 -lt $resolved.Length -and $resolved[$i + 1] -eq '"') {
                [void]$current.Append('"'); $i += 2; continue
            }
            $inQuotes = -not $inQuotes; $hadQuote = $true; $i++; continue
        }
        if (($c -eq ' ' -or $c -eq "`t") -and -not $inQuotes) {
            # MSVC: a quoted run yields a token even when EMPTY ("").
            if ($current.Length -gt 0 -or $hadQuote) {
                $tokens.Add($current.ToString()); [void]$current.Clear()
                $hadQuote = $false
            }
            $i++; continue
        }
        [void]$current.Append($c); $i++
    }
    if ($current.Length -gt 0 -or $hadQuote) { $tokens.Add($current.ToString()) }
    return @($tokens)
}

function New-BrokerTaskDefinitionXml {
    # R62-D: the task definition is a LEGAL XML document, not CLI enums.
    # ONSTART (production default) = exactly one BootTrigger; ONDEMAND
    # (experiment) = NO trigger elements at all - the task can only be
    # started explicitly (schtasks /Run), never at boot. AllowStartOnDemand
    # is a Task SETTING (true), independent of whether a trigger exists.
    # Pure function: deterministic output so the definition file is
    # hash-verifiable as an install artifact.
    param([hashtable]$Cfg, [string]$WrapperPath)
    $triggerXml = ''
    if ([string]$Cfg['TaskTrigger'] -eq 'ONSTART') {
        $triggerXml = '  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>'
    } else {
        # ONDEMAND: no <Triggers> element at all - a definition without
        # triggers never starts automatically; /Run is the only start path.
        $triggerXml = '  <Triggers />'
    }
    # The Exec arguments element stays EMPTY on purpose: the generated
    # wrapper (run-broker.cmd) owns the FULL broker launch contract -
    # including --watchdog-script/--run-root/--firewall-rule/
    # --share-faults-file/--dispatch-deadline - and forwards the operator
    # tail %* unchanged. Duplicating flags here would double-pass them.
    return @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>UM share broker host (generated by install-share-broker; paths bound to this install)</Description>
  </RegistrationInfo>
__TRIGGERS__
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
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
      <WorkingDirectory>__WORKDIR__</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'@.Replace('__TRIGGERS__', $triggerXml).Replace('__COMMAND__', $WrapperPath).Replace('__WORKDIR__', [string]$Cfg['RunRoot'])
}

function Register-BrokerTask {
    # R49-4: re-check the CURRENT task definition RIGHT BEFORE the
    # create (the inventory verdict may be stale - a foreign task
    # appearing in between is NEVER overwritten): absent -> create
    # WITHOUT /F (a non-overwriting create is the race guard);
    # owned+identical -> idempotent reuse (no rebuild); anything
    # else refuses.
    param([hashtable]$Cfg, [string]$WrapperPath, [scriptblock]$SchtasksRunner,
          [bool]$PrevOwned)
    $facts = Get-BrokerTaskFacts -Cfg $Cfg -SchtasksRunner $SchtasksRunner `
        -WrapperPath $WrapperPath -PrevOwned $PrevOwned
    if ($null -eq $facts.Exists) {
        throw ("register refused: task identity could not be established (" + $facts.Reason + ")")
    }
    if ($facts.Exists -and -not $facts.Owned) {
        throw ("register refused: task '" + $Cfg['TaskName'] + "' appeared/changed and is not owned by this install (" + $facts.Reason + ")")
    }
    # R53.3: the wrapper CONTENT is bound to THIS install's generated
    # result before registration - a file that appeared or was
    # replaced after the inventory is never registered as a SYSTEM
    # launch entry without content verification.
    $expectedContent = New-BrokerWrapperContent -Cfg $Cfg
    if (Test-Path -LiteralPath $WrapperPath -PathType Leaf) {
        $actualContent = [IO.File]::ReadAllText($WrapperPath)
        if ($actualContent -ne $expectedContent) {
            throw ("register refused: wrapper content differs from the generated result (replaced after inventory?): " + $WrapperPath)
        }
    } else {
        throw ("register refused: wrapper file missing at registration time: " + $WrapperPath)
    }
    # R62-D: registration consumes the LEGAL definition document (no /SC
    # enums). The definition file is THIS install's artifact and is bound by
    # content: a replaced/foreign definition file refuses registration.
    $defPath = Join-Path $Cfg['RunRoot'] 'broker\task-definition.xml'
    if (-not (Test-Path -LiteralPath $defPath -PathType Leaf)) {
        throw ("register refused: task definition file missing (written by the install before registration): " + $defPath)
    }
    $expectedDef = New-BrokerTaskDefinitionXml -Cfg $Cfg -WrapperPath $WrapperPath
    $actualDef = [IO.File]::ReadAllText($defPath)
    if ($actualDef -ne $expectedDef) {
        throw ("register refused: task definition content differs from the generated result (replaced after inventory?): " + $defPath)
    }
    if ($facts.Exists -and $facts.Owned) {
        Write-Log ("task already registered with THIS definition; reusing idempotently: " + $Cfg['TaskName'])
        return
    }
    # R62-D: /Create /XML registers the reviewed definition verbatim - no
    # CLI schedule-type enum is passed; the XML decides triggers and
    # identity. schtasks /SC ONDEMAND is NOT a legal schedule type.
    $result = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Create', '/TN', $Cfg['TaskName'], '/XML', $defPath)
    if ($result.ExitCode -ne 0) {
        throw ("schtasks create failed (exit " + $result.ExitCode + "): " + ($result.Output -join ' '))
    }
    Write-Log ("registered SYSTEM task " + $Cfg['TaskName'] + " -> " + $WrapperPath)
}

function Resolve-D4TaskPrincipalSid {
    # §210.3 W1: REAL principal resolution SHARED by every task-shape verifier
    # (Test-D4TaskDefinitionShape + Get-BrokerTaskFacts). This lib is
    # dot-sourced by BOTH the standalone broker installer (install-share-
    # broker.ps1) and execute-d4-experiment-lib.ps1, so the SAME identity
    # semantics serve every principal comparison. The re-exported <UserId> is
    # EITHER a canonical SID string (S-1-5-21-... or a well-known SID such as
    # S-1-5-18) OR a Windows ACCOUNT NAME (short local name, MACHINE-name,
    # dot-name, or a well-known service name like 'SYSTEM'). A SID literal is
    # parsed directly. An account name is resolved through the REAL Windows
    # SID-translation API (NTAccount -> SID lookup) and, when the name is a
    # local account, CROSS-CHECKED against the local account database - a
    # name can never pass as a different identity and a resolution failure /
    # ambiguity is a refusal (unconfirmed), never a legal value. Group
    # principals are rejected by the verifiers (no UserId); a name that
    # resolves to a non-local principal (well-known service like SYSTEM) is
    # returned as-is and the expected-SID comparison decides.
    param([string]$PrincipalText)
    if ([string]::IsNullOrWhiteSpace($PrincipalText)) { return @{ ok = $false; sid = ''; reason = 'empty principal' } }
    $t = $PrincipalText.Trim()
    if ([regex]::IsMatch($t, '^S-1-5-')) {
        try {
            $sidObj = New-Object System.Security.Principal.SecurityIdentifier($t)
            return @{ ok = $true; sid = [string]$sidObj.Value; form = 'sid' }
        } catch { }
    }
    $name = $t
    if ($name.StartsWith('.\', [System.StringComparison]::Ordinal)) { $name = $name.Substring(2) }
    $sidVal = ''
    try {
        $nt = New-Object System.Security.Principal.NTAccount($name)
        $s = $nt.Translate([System.Security.Principal.SecurityIdentifier])
        $sidVal = [string]$s.Value
    } catch { return @{ ok = $false; sid = ''; reason = ('name resolution failed: ' + $_.Exception.Message) } }
    $lu = $null
    try { $lu = Get-LocalUser -Name $name -ErrorAction Stop } catch { $lu = $null }
    if ($null -ne $lu) {
        if ([string]$lu.SID.Value -cne $sidVal) { return @{ ok = $false; sid = ''; reason = ('local-account evidence contradicts the name->SID resolution (' + $sidVal + ' != ' + [string]$lu.SID.Value + ')') } }
        return @{ ok = $true; sid = $sidVal; form = 'local-user' }
    }
    return @{ ok = $true; sid = $sidVal; form = 'non-local' }
}

function Get-BrokerTaskFacts {
    # D1 (R46.7): the task identity is parsed STRUCTURALLY from the task
    # DEFINITION (schtasks /Query /TN <name> /XML - non-localized): the
    # Exec action's Command must equal THIS install's wrapper path and the
    # Arguments must target this broker's server entry. Description or
    # other free text containing the wrapper path is NOT ownership
    # evidence; a previous install record can never override a current
    # definition conflict. Query FAILURE is never "task absent" - only the
    # explicit "cannot find" evidence counts as absence.
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [string]$WrapperPath,
          [bool]$PrevOwned)
    $query = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Query', '/TN', $Cfg['TaskName'], '/XML')
    $text = (@($query.Output) -join "`n")
    if ($query.ExitCode -ne 0) {
        if ($text -match 'cannot find|找不到') {
            return @{ Exists = $false; Owned = $false; Reason = 'task absent' }
        }
        return @{ Exists = $null; Owned = $false;
                  Reason = ("schtasks /Query failed (exit " + $query.ExitCode + ")") }
    }
    if (-not $text.Trim()) {
        return @{ Exists = $null; Owned = $false;
                  Reason = 'task query produced no readable definition' }
    }
    $action = $null
    $arguments = ''
    try {
        $xml = [xml]$text
        # R51.5: the LEGAL task shape is exactly ONE Exec whose
        # arguments are EMPTY (this install's /TR carries the wrapper
        # only), the expected principal (SYSTEM by default), and no
        # WorkingDirectory. Any other shape is a foreign definition,
        # never silently adopted.
        # R53.4: check ALL action elements, not just Exec - a legal Exec
        # beside a ComHandler/ShowMessage/SendEmail/unknown element is
        # a mixed definition that must refuse.
        $actionElements = @()
        foreach ($child in $xml.Task.Actions.ChildNodes) {
            if ($child.NodeType -ne 'Element') { continue }
            $actionElements += $child.LocalName
        }
        if ($actionElements.Count -ne 1 -or $actionElements[0] -ne 'Exec') {
            return @{ Exists = $null; Owned = $false;
                      Reason = ('task defines ' + $actionElements.Count +
                                ' action(s) of type(s) [' + ($actionElements -join ',') +
                                ']; expected exactly 1 Exec') }
        }
        $execNodes = @($xml.Task.Actions.Exec)
        # R53.4: exactly ONE Principal expected (multiple run identities
        # are a foreign definition, never silently adopted).
        $principalNodes = @($xml.Task.Principals.Principal)
        if ($principalNodes.Count -ne 1) {
            return @{ Exists = $null; Owned = $false;
                      Reason = ('task defines ' + $principalNodes.Count + ' principals; expected exactly 1') }
        }
        # R62-D: an ONDEMAND deployment's definition must carry NO trigger
        # elements - any automatic trigger means a foreign definition that
        # could start at boot without a separate decision.
        if ([string]$Cfg['TaskTrigger'] -eq 'ONDEMAND') {
            $triggerElements = @()
            if ($xml.Task.Triggers) {
                $triggerElements = @($xml.Task.Triggers.ChildNodes |
                    Where-Object { $_.NodeType -eq 'Element' })
            }
            if ($triggerElements.Count -ne 0) {
                return @{ Exists = $null; Owned = $false;
                          Reason = ('task defines ' + $triggerElements.Count +
                                    ' trigger(s) [' + (($triggerElements | ForEach-Object { $_.LocalName }) -join ',') +
                                    ']; expected on-demand only (no automatic start)') }
            }
        }
        $action = [string]$execNodes[0].Command
        $arguments = [string]$execNodes[0].Arguments
        $workDir = [string]$execNodes[0].WorkingDirectory
        $principalNode = $xml.Task.Principals.Principal
        # §210.3 W1: the task identity is USER-scoped; a GroupId-only
        # (group-principal) export is NEVER treated as a User principal.
        $principalUserId = [string]$principalNode.UserId
        if (-not $principalUserId) {
            return @{ Exists = $true; Owned = $false;
                      Reason = ('task principal has no UserId (group principal refused): ' + [string]$principalNode.GroupId) }
        }
    } catch {
        return @{ Exists = $null; Owned = $false;
                  Reason = ("task definition unparsable: " + $_.Exception.Message) }
    }
    if (-not $action) {
        return @{ Exists = $null; Owned = $false;
                  Reason = 'task definition has no Exec action' }
    }
    if ($arguments) {
        # R51.5: this install creates the task with an EMPTY argument
        # tail (/TR carries the wrapper only) - any arguments mean a
        # different definition.
        return @{ Exists = $true; Owned = $false;
                  Reason = 'task action carries arguments this install does not create' }
    }
    if ($workDir) {
        # W4 defect @4: this install now pins the task WorkingDirectory to
        # the install root (in addition to the wrapper's cd); a task whose
        # WorkingDirectory differs is a foreign definition.
        if ($workDir -ne [string]$Cfg['RunRoot']) {
            return @{ Exists = $true; Owned = $false;
                      Reason = 'task defines a WorkingDirectory this install does not create' }
        }
    }
    $expectedPrincipal = 'S-1-5-18'
    if ($Cfg['TaskPrincipal']) { $expectedPrincipal = $Cfg['TaskPrincipal'] }
    # §210.3 W1: resolve the EXPORTED principal (a SID literal or an account
    # name) to a canonical SID through the SHARED real resolution edge, then
    # compare SID to SID (never a name-string == SID-string comparison).
    $resolvedPrincipal = Resolve-D4TaskPrincipalSid -PrincipalText $principalUserId
    if (-not $resolvedPrincipal.ok) {
        return @{ Exists = $true; Owned = $false;
                  Reason = ('principal resolution failed/ambiguous (' + $principalUserId + '): ' + [string]$resolvedPrincipal.reason) }
    }
    if ([string]$resolvedPrincipal.sid -cne $expectedPrincipal) {
        return @{ Exists = $true; Owned = $false;
                  Reason = ('task principal differs from the expected run identity (got ' + [string]$resolvedPrincipal.sid + ')') }
    }
    if ($action.Trim('"').ToLowerInvariant() -ne
            $WrapperPath.Trim('"').ToLowerInvariant()) {

        # D1: the ACTUAL action executable differs from this install's
        # wrapper - ownership refused no matter what any record says.
        return @{ Exists = $true; Owned = $false;
                  Reason = 'same-name task action differs from this install wrapper' }
    }
    # Arguments are the WRAPPER's own parameter tail (the server entry
    # lives inside the wrapper, not in the task action) - recorded but not
    # an ownership criterion.
    if ($PrevOwned) {
        return @{ Exists = $true; Owned = $true;
                  Reason = 'task action matches this wrapper + previous record' }
    }
    return @{ Exists = $true; Owned = $true;
              Reason = 'task action references this install wrapper' }
}

function Get-BrokerProcessPids {
    # D5: broker-process evidence by ACTUAL COMMAND LINE (CIM query by the
    # image name; the row counts as the broker only when its command line
    # runs share_broker_server.py). $null = evidence failure (unconfirmed
    # - never treated as "no broker"); an empty result from a succeeded
    # query is the explicit no-broker evidence.
    param([hashtable]$Cfg, [scriptblock]$Runner)
    $filter = 'Name=' + "'" + $Cfg['ShareImage'] + "'"
    $parts = @('$ErrorActionPreference = ''Stop''; try { '
               'Get-CimInstance Win32_Process -Filter ', $filter,
               ' | ForEach-Object { '
               'try { $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop } catch { $o = $null }; '
               '[pscustomobject]@{ ProcessId = $_.ProcessId; CommandLine = $_.CommandLine; '
               'ExecutablePath = $_.ExecutablePath; CreationDate = $_.CreationDate; '
               'OwnerDomain = $o.Domain; OwnerUser = $o.User } } | '
               'ConvertTo-Json -Compress } catch { exit 1 }')
    $script = -join $parts
    $result = Invoke-CommandRunner -Runner $Runner -ArgumentList @(
        'powershell', '-NoProfile', '-Command', $script)
    # Returns a STRUCTURED record (an empty array would be unrolled to
    # $null by PowerShell, making 'evidence failure' indistinguishable
    # from 'no broker'): @{ Unconfirmed = bool; Pids = int[] }.
    if ($result.ExitCode -ne 0) { return @{ Unconfirmed = $true; Pids = @() } }
    $stdout = (@($result.Output) -join "`n").Trim()
    if (-not $stdout) { return @{ Unconfirmed = $false; Pids = @() } }
    try {
        $payload = $stdout | ConvertFrom-Json
    } catch { return @{ Unconfirmed = $true; Pids = @() } }
    if ($null -eq $payload -or $payload -is [bool]) {
        # R51.4: a JSON null/false payload is NOT empty evidence -
        # it is an unusable query result.
        return @{ Unconfirmed = $true; Pids = @() }
    }
    $rows = @($payload)
    $pids = @()
    foreach ($row in $rows) {
        # R51.4: the broker evidence needs the SAME full-record shape
        # as the share side (PID + command line + FULL image path +
        # creation + owner); any missing field is evidence failure.
        $procPid = [int]$row.ProcessId
        $cmdline = [string]$row.CommandLine
        $imagePath = [string]$row.ExecutablePath
        $creation = [string]$row.CreationDate
        $owner = ([string]$row.OwnerDomain, [string]$row.OwnerUser)
        if ($procPid -le 0 -or -not $cmdline -or -not $imagePath -or
                -not $creation -or -not $owner[0] -or -not $owner[1]) {
            return @{ Unconfirmed = $true; Pids = @() }
        }
        if ($cmdline -match 'share_broker_server') {
            $pids += $procPid
        }
    }
    return @{ Unconfirmed = $false; Pids = $pids }
}

function Confirm-TaskStopped {
    # R44-6/D5: query FIRST; only the explicit "cannot find" evidence
    # counts as absent - any other non-zero exit is a REFUSAL, never a
    # pretend absence. A Running task is /End-ed and re-verified. AND the
    # stop confirmation now needs BROKER-PROCESS EXIT evidence as well:
    # a non-Running task string alone can never prove the broker left
    # (an orphaned process or a PID-reused survivor must refuse), and a
    # failed process query stays unconfirmed instead of passing.
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner)
    $query = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Query', '/TN', $Cfg['TaskName'], '/FO', 'CSV', '/NH')
    $text = (@($query.Output) -join "`n")
    if ($query.ExitCode -ne 0) {
        if ($text -match 'cannot find|找不到') {
            return (Confirm-BrokerProcessExited -Cfg $Cfg -Runner $SchtasksRunner `
                -Reason 'task absent')
        }
        return @{ Ok = $false;
                  Reason = ("schtasks /Query failed (exit " + $query.ExitCode + "); refusal, not absence") }
    }
    $row = ($query.Output | Where-Object { $_ -and "$_".Trim().StartsWith([char]34) }) | Select-Object -First 1
    if (-not $row) {
        return @{ Ok = $false; Reason = 'task query row unparsable' }
    }
    $state = ("$row" -split '","')[-1].Trim('"')
    if ($state -ne 'Running') {
        return (Confirm-BrokerProcessExited -Cfg $Cfg -Runner $SchtasksRunner `
            -Reason ("task state: " + $state))
    }
    $end = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/End', '/TN', $Cfg['TaskName'])
    if ($end.ExitCode -ne 0) {
        return @{ Ok = $false; Reason = ("schtasks /End failed (exit " + $end.ExitCode + ")") }
    }
    $query2 = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Query', '/TN', $Cfg['TaskName'], '/FO', 'CSV', '/NH')
    $text2 = (@($query2.Output) -join "`n")
    if ($query2.ExitCode -ne 0) {
        if ($text2 -match 'cannot find|找不到') {
            return (Confirm-BrokerProcessExited -Cfg $Cfg -Runner $SchtasksRunner `
                -Reason 'task absent after /End')
        }
        return @{ Ok = $false;
                  Reason = ("schtasks /Query after /End failed (exit " + $query2.ExitCode + ")") }
    }
    $row2 = ($query2.Output | Where-Object { $_ -and "$_".Trim().StartsWith([char]34) }) | Select-Object -First 1
    if (-not $row2) {
        return @{ Ok = $false; Reason = 'task query row unparsable after /End' }
    }
    $state2 = ("$row2" -split '","')[-1].Trim('"')
    if ($state2 -eq 'Running') {
        return @{ Ok = $false; Reason = 'task still Running after /End' }
    }
    return (Confirm-BrokerProcessExited -Cfg $Cfg -Runner $SchtasksRunner `
        -Reason ("task state after /End: " + $state2))
}

function Confirm-BrokerProcessExited {
    # D5: the FINAL stop evidence is the broker PROCESS being gone (the
    # task state string alone never proves it). Evidence failure or a
    # live broker process refuses; the explicit empty result confirms.
    param([hashtable]$Cfg, [scriptblock]$Runner, [string]$Reason)
    $record = Get-BrokerProcessPids -Cfg $Cfg -Runner $Runner
    if ($record.Unconfirmed) {
        return @{ Ok = $false;
                  Reason = ($Reason + "; broker process evidence unconfirmed") }
    }
    $pids = @($record.Pids)
    if ($pids.Count -gt 0) {
        $pidText = ($pids -join ',')
        return @{ Ok = $false;
                  Reason = ($Reason + "; broker process still alive (pids: " + $pidText + ")") }
    }
    return @{ Ok = $true; Reason = ($Reason + "; broker process gone") }
}

function Remove-BrokerTask {
    # R44-6: a FAILED /Delete refuses; after a successful /Delete only the
    # explicit "cannot find" query evidence counts as deleted - any other
    # query outcome refuses (a query failure is NOT "task gone").
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner)
    $del = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Delete', '/TN', $Cfg['TaskName'], '/F')
    if ($del.ExitCode -ne 0) {
        return @{ Ok = $false; Reason = ("schtasks /Delete failed (exit " + $del.ExitCode + ")") }
    }
    $query = Invoke-CommandRunner -Runner $SchtasksRunner -ArgumentList @(
        'schtasks', '/Query', '/TN', $Cfg['TaskName'], '/FO', 'CSV', '/NH')
    $text = (@($query.Output) -join "`n")
    if ($query.ExitCode -ne 0) {
        if ($text -match 'cannot find|找不到') {
            return @{ Ok = $true; Reason = 'task deleted' }
        }
        return @{ Ok = $false; Reason = ("post-delete query failed (exit " + $query.ExitCode + ")") }
    }
    return @{ Ok = $false; Reason = 'task still present after delete' }
}

function Write-InstallState {
    param([hashtable]$Cfg, [object[]]$Modules, [string]$StatePath,
          [string]$WrapperRel, [hashtable]$PreDir)
    $state = [ordered]@{
        task_name = $Cfg['TaskName']
        share_task = $Cfg['ShareTaskName']
        run_root = $Cfg['RunRoot']
        broker_pipe_name = $Cfg['BrokerPipeName']
        instance_pipe_prefix = $Cfg['InstancePipePrefix']
        operator_account = $Cfg['OperatorAccount']
        python_home = $Cfg['PythonHome']
        share_port = $Cfg['SharePort']
        task_wrapper = $WrapperRel
        pre_dir_state = $PreDir
        modules = @($Modules)
        installed_at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    }
    $tmp = $StatePath + '.tmp'
    [IO.File]::WriteAllText($tmp, ($state | ConvertTo-Json -Depth 4),
        (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $StatePath -Force
    Write-Log ("install-state.json written (" + @($Modules).Count + " manifest entries)")
}

function New-BrokerConfig {
    # Normalizes the entry parameters into ONE deployment config. Every
    # namespace knob (root, task names, BOTH pipe names) is explicit here -
    # an experiment passes its own values and the generated artifacts can
    # then never reference the production namespace.
    param(
        [string]$RunRoot,
        [string]$SourceRoot,
        [string]$TaskName,
        [string]$ShareTaskName,
        [string]$BrokerPipeName,
        [string]$InstancePipePrefix,
        [string]$OperatorAccount,
        [string]$InstanceOwnerSid,
        [string]$BrokerPipeSddl,
        [string]$PythonHome,
        [int]$SharePort = 8100,
        [string]$ShareImage = 'um-share-python.exe',
        [string]$ExpectedOwnerAccount = '',
        # P3 (D4-P §60.3): the broker task trigger is EXPLICIT. Production
        # keeps the boot trigger (default unchanged); the experiment uses
        # ONDEMAND so nothing starts at boot without a separate decision.
        [ValidateSet('ONSTART', 'ONDEMAND')][string]$TaskTrigger = 'ONSTART'
    )
    if (-not $RunRoot) { throw 'RunRoot is required' }
    if (-not $TaskName) { throw 'TaskName is required' }
    if (-not $ShareTaskName) { throw 'ShareTaskName is required' }
    if (-not $BrokerPipeName) { throw 'BrokerPipeName is required' }
    if (-not $InstancePipePrefix) { throw 'InstancePipePrefix is required' }
    if (-not $OperatorAccount) { throw 'OperatorAccount is required' }
    if (-not $PythonHome) { throw 'PythonHome is required' }
    if ($SharePort -lt 1 -or $SharePort -gt 65535) {
        throw 'SharePort must be 1..65535'
    }
    return @{
        RunRoot = $RunRoot
        SourceRoot = $SourceRoot
        TaskName = $TaskName
        ShareTaskName = $ShareTaskName
        BrokerPipeName = $BrokerPipeName
        InstancePipePrefix = $InstancePipePrefix
        OperatorAccount = $OperatorAccount
        InstanceOwnerSid = $InstanceOwnerSid
        BrokerPipeSddl = $BrokerPipeSddl
        PythonHome = $PythonHome
        SharePort = $SharePort
        ShareImage = $ShareImage
        ExpectedOwnerAccount = $ExpectedOwnerAccount
        TaskTrigger = $TaskTrigger
    }
}

function Test-Prerequisites {
    # P3 (D4-P §60.2): the install gate checks DEPLOYMENT FACTS whose
    # producers are defined - never a hand-writable transient status file:
    #   deploy-mode.json         <- operator/deployment, explicit and never
    #                               inferred (the guard refuses without it;
    #                               same gate as deploy-elevated Get-DeployMode)
    #   um-share-projection.json <- the single-authority projection writer
    #                               (existence required when mode=projection)
    #   um-share-python.exe      <- deploy Prep Copy-ShareSource hardlink
    # guard-status.json (whatever --run-dir the guard was started with) is a
    # per-run TRANSIENT written by the guard process itself - it is NOT an
    # install prerequisite, so the install can complete BEFORE any process
    # start; the actual start keeps its own guard/limit/identity gates.
    param([hashtable]$Cfg)
    $appExe = Join-Path $Cfg['RunRoot'] 'um-share-python.exe'
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
        throw ("um-share-python.exe hardlink missing in RunRoot (owned by deploy-elevated.ps1 Copy-ShareSource): " + $appExe)
    }
    $modeFile = Join-Path $Cfg['RunRoot'] 'deploy-mode.json'
    if (-not (Test-Path -LiteralPath $modeFile -PathType Leaf)) {
        throw ("deployment mode file missing: " + $modeFile +
               " - the share deployment must exist first (the mode is explicit, never inferred)")
    }
    $mode = $null
    try {
        $mode = ([IO.File]::ReadAllText($modeFile) | ConvertFrom-Json).mode
    } catch {
        throw ("deployment mode file is corrupt: " + $_.Exception.Message)
    }
    if ($mode -ne 'projection' -and $mode -ne 'legacy') {
        throw ("deployment mode must be 'projection' or 'legacy', got '" + $mode + "'")
    }
    if ($mode -eq 'projection') {
        $derived = Join-Path $Cfg['RunRoot'] 'um-share-projection.json'
        if (-not (Test-Path -LiteralPath $derived -PathType Leaf)) {
            throw ("projection-mode deployment but the derived projection is missing: " +
                   $derived + "; refusing to install (a lost projection must never " +
                   "silently re-introduce the old authorizations)")
        }
    }
    foreach ($m in (Get-BrokerSourceManifest -SourceRoot $Cfg['SourceRoot'])) {
        if (-not (Test-Path -LiteralPath $m.Src -PathType Leaf)) {
            throw ("broker source missing: " + $m.Src)
        }
    }
}

function Set-ManagedEntryState {
    # R49-2: upgrade ONE manifest entry IN PLACE after its successful
    # write (atomic rewrite of install-state.json): planned -> created
    # with the ACTUAL content hash. Every write point calls this
    # immediately, so a crash between write and record can never leave an
    # empty-hash owned entry.
    param([string]$StatePath, [string]$File, [string]$Classification,
          [string]$Sha256)
    $state = [IO.File]::ReadAllText($StatePath) | ConvertFrom-Json
    $final = @()
    foreach ($m in @($state.modules)) {
        if ($m.file -eq $File) {
            $m.classification = $Classification
            $m.sha256 = $Sha256
        }
        $final += $m
    }
    $state.modules = @($final)
    $tmp = $StatePath + '.tmp'
    [IO.File]::WriteAllText($tmp, ($state | ConvertTo-Json -Depth 4),
        (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $StatePath -Force
}

function Get-AclRecoveryRecord {
    # §57: the ONE parsing contract for the manifest acl_recovery record,
    # shared by install reentry and uninstall recovery. A manifest WITHOUT
    # the acl_recovery property is supported legacy -> $null. A record that
    # EXISTS must be an object carrying a legal non-empty state enum
    # (pending/failed/none); pending/failed additionally require a non-empty
    # backup path. Anything else throws - a corrupted record is never
    # silently reinterpreted as legacy or none, and never overwritten by a
    # fresh install manifest.
    param([object]$Manifest)
    $prop = $Manifest.PSObject.Properties['acl_recovery']
    if ($null -eq $prop) { return $null }
    $obj = $prop.Value
    if ($null -eq $obj -or $obj -is [string] -or $obj -isnot [pscustomobject]) {
        throw ("corrupted acl_recovery record (not an object): " + [string]$obj)
    }
    $state = [string]$obj.state
    $shown = $state
    if (-not $state) { $shown = '<missing/empty>' }
    if ($state -notin 'pending', 'failed', 'none') {
        throw ("unknown ACL recovery state: " + $shown)
    }
    $backup = [string]$obj.backup
    if (($state -eq 'pending' -or $state -eq 'failed') -and -not $backup) {
        throw ("corrupted acl_recovery record: state " + $state + " requires a backup path")
    }
    return @{ State = $state; Backup = $backup }
}

function Set-AclRecoveryState {
    # R53.2: the ACL recovery STATE is durable manifest fact
    # (pending = a backup exists and the ACL may have changed;
    # none = consumed/clean; failed = restore failed, evidence
    # kept). Atomic rewrite like Write-InstallState.
    param([string]$StatePath, [string]$State, [string]$BackupRel)
    # NOTE: the manifest object is read into $manifestObj - a local named
    # $state would COLLIDE with the $State parameter (PowerShell variable
    # names are case-insensitive) and clobber it (R53.2 root cause).
    $manifestObj = [IO.File]::ReadAllText($StatePath) | ConvertFrom-Json
    # R53.2: rebuild as a PLAIN hashtable (the Add-Member/ordered
    # combination wrote a JSON-STRINGIFIED hashtable ToString in this
    # environment - the rebuild is the supported serialization path).
    $stateObj = @{}
    foreach ($prop in $manifestObj.PSObject.Properties) { $stateObj[$prop.Name] = $prop.Value }
    $stateObj['acl_recovery'] = @{ state = [string]$State; backup = [string]$BackupRel }
    $tmp = $StatePath + '.tmp'
    [IO.File]::WriteAllText($tmp, ($stateObj | ConvertTo-Json -Depth 6),
        (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $StatePath -Force
}

function Undo-AclRecoveryState {
    # R53.2: roll back a possibly-changed ACL via the pending backup
    # (icacls /restore). Success -> backup consumed, state none.
    # Failure -> state failed, backup KEPT (evidence), and the
    # caller must not continue.
    param([string]$BrokerDir, [hashtable]$Cfg, [scriptblock]$IcaclsRunner, [string]$StatePath)
    $directBackup = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath 'broker-pre-acl.dat'
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        # R53.2/R51.2: a crash can leave the backup WITHOUT a manifest
        # (killed before Phase 0.5). Its ACL-change state is UNKNOWN -
        # auto-applying it could roll back an unrelated change, so the
        # install REFUSES and leaves the material for operator review.
        if (Test-Path -LiteralPath $directBackup -PathType Leaf) {
            throw ("orphan ACL backup without an ownership record requires operator review: " + $directBackup)
        }
        return
    }
    $state = [IO.File]::ReadAllText($StatePath) | ConvertFrom-Json
    # §57: the SAME record contract as install - a manifest without the
    # field is legacy (nothing to recover); an existing record with a
    # missing/empty/illegal state refuses here too (never "treated as none").
    $rec = Get-AclRecoveryRecord -Manifest $state
    if ($null -eq $rec) { return }
    if ($rec.State -notin 'pending', 'failed') {
        return
    }
    $backupPath = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $rec.Backup
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        Write-Log ("WARNING: acl backup missing while state=" + $rec.State +
                   "; checked: " + $backupPath + "; operator review required")
        Set-AclRecoveryState -StatePath $StatePath -State 'failed' -BackupRel $rec.Backup
        return
    }
    $restore = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
        'icacls', $BrokerDir, '/restore', $backupPath)
    if ($restore.ExitCode -eq 0) {
        # R55.4: record the terminal state FIRST (so a crash between
        # recording and the backup deletion leaves a consistent
        # 'none' state - the recovery is done, the file is GC).
        Set-AclRecoveryState -StatePath $StatePath -State 'none' -BackupRel ''
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Remove-Item -LiteralPath $backupPath -Force
        }
        Write-Log "ACL recovery consumed (icacls /restore)"
    } else {
        Set-AclRecoveryState -StatePath $StatePath -State 'failed' -BackupRel ([string]$rec.backup)
        Write-Log ("WARNING: ACL rollback FAILED (exit " + $restore.ExitCode + "); backup kept: " + $backupPath)
    }
}

function Install-Broker {
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [scriptblock]$IcaclsRunner)
    Test-Prerequisites -Cfg $Cfg
    $brokerDir = Join-Path $Cfg['RunRoot'] 'broker'
    $statePath = Join-Path $brokerDir 'install-state.json'

    # ---------------- Phase 0: FULL inventory, ZERO side effects ----------
    # Every module destination, both keys, the wrapper and the same-name
    # task are classified BEFORE any write; ANY conflict refuses the whole
    # install (R44-5: an absent-then-conflict sequence must not copy).
    $prevOwned = @{}
    $prevTaskOwned = $false
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try {
            $prev = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
            foreach ($pm in @($prev.modules)) {
                # R49-2: 'created' entries carry a recorded content
                # hash - they are THIS install's prior ownership too.
                if ($pm.classification -in 'owned', 'created') { $prevOwned[$pm.file] = $pm.classification }
            }
            $prevTaskOwned = ([string]$prev.task_name -eq $Cfg['TaskName'])
        } catch {
            throw ("previous install-state.json unreadable; refusing to install over an unreadable ownership record: " + $_.Exception.Message)
        }
    }
    $plan = @()
    $srcByFile = @{}
    foreach ($m in (Get-BrokerSourceManifest -SourceRoot $Cfg['SourceRoot'])) {
        $srcByFile[$m.File] = $m.Src
        $dst = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $m.File
        if (Test-Path -LiteralPath $dst -PathType Leaf) {
            $dstHash = Get-FileSha256 $dst
            $srcHash = Get-FileSha256 $m.Src
            if ($dstHash -ne $srcHash) {
                throw ("destination conflict for " + $m.File + ": existing copy differs from the broker source; update the share deployment first (install refused, nothing overwritten)")
            }
            $cls = 'shared'
            if ($prevOwned.ContainsKey($m.File)) { $cls = $prevOwned[$m.File] }
            $plan += @{ file = $m.File; sha256 = $srcHash; classification = $cls }
            Write-Log ("pre-existing identical, " + $cls + ": " + $m.File)
            continue
        }
        # R49-2: an absent module is PLANNED (intent with the source
        # hash as the expectation); it becomes created after the
        # verified copy, not before.
        $plan += @{ file = $m.File; sha256 = (Get-FileSha256 $m.Src); classification = 'planned' }
    }
    foreach ($keyRel in @('broker\broker-control-key', 'instance-channel-key')) {
        $dst = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $keyRel
        if (Test-Path -LiteralPath $dst -PathType Leaf) {
            $existing = ([IO.File]::ReadAllText($dst)).Trim()
            if ($existing -notmatch '^[0-9a-f]{64}$') {
                throw ("pre-existing key is not a 64-hex value: " + $keyRel)
            }
            $cls = 'shared'
            if ($prevOwned.ContainsKey($keyRel)) { $cls = $prevOwned[$keyRel] }
            # R51.2: pre-existing (prev-owned) keys record their CURRENT
            # content hash - a reinstall then hash-checks like any
            # created entry; absent keys stay planned with empty hash.
            $keyHash = ''
            if ($cls -in 'owned', 'created') { $keyHash = Get-FileSha256 $dst }
            $plan += @{ file = $keyRel; sha256 = $keyHash; classification = $cls }
            Write-Log ("pre-existing key kept, " + $cls + ": " + $keyRel)
        } else {
            $plan += @{ file = $keyRel; sha256 = ''; classification = 'planned' }
        }
    }
    $wrapperContent = New-BrokerWrapperContent -Cfg $Cfg
    $wrapperRel = 'run-broker.cmd'
    $wrapperPath = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $wrapperRel
    if (Test-Path -LiteralPath $wrapperPath -PathType Leaf) {
        $existing = [IO.File]::ReadAllText($wrapperPath)
        if ($existing -ne $wrapperContent) {
            throw "pre-existing run-broker.cmd differs from the generated wrapper; refusing to overwrite unknown content"
        }
        $cls = 'shared'
        if ($prevOwned.ContainsKey($wrapperRel)) { $cls = $prevOwned[$wrapperRel] }
        $plan += @{ file = $wrapperRel; sha256 = (Get-FileSha256 $wrapperPath); classification = $cls }
        Write-Log ("pre-existing identical wrapper, " + $cls)
    } else {
        $plan += @{ file = $wrapperRel; sha256 = ''; classification = 'planned' }
    }
    # R62-D: the task DEFINITION document is an install artifact in the
    # manifest - planned with its EXPECTED content hash, created after the
    # verified write, never silently adopted from a foreign file.
    $taskDefRel = 'broker\task-definition.xml'
    $taskDefPath = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $taskDefRel
    $taskDefExpected = New-BrokerTaskDefinitionXml -Cfg $Cfg -WrapperPath $wrapperPath
    $taskDefHash = [System.Security.Cryptography.SHA256]::Create()
    try {
        # The inventoried hash must cover the FILE bytes: the definition is
        # written UTF-16LE WITH BOM (schtasks/MSXML refuses a BOM-less UTF-16
        # XML - the real /Create rejected exactly that shape), so the BOM
        # preamble is part of the expected content.
        $defBytes = ([Text.Encoding]::Unicode.GetPreamble() +
                     [Text.Encoding]::Unicode.GetBytes($taskDefExpected))
        $expectedDefHash = ([BitConverter]::ToString($taskDefHash.ComputeHash($defBytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $taskDefHash.Dispose()
    }
    if (Test-Path -LiteralPath $taskDefPath -PathType Leaf) {
        # Same manifest discipline as the wrapper (R41-5/R41-6): a pre-existing
        # definition with IDENTICAL content is this install's own prior
        # artifact (shared, or prev-owned per the record) and is kept; a
        # DIFFERENT one is foreign and refuses - never overwritten.
        $existingDef = [IO.File]::ReadAllText($taskDefPath)
        if ($existingDef -ne $taskDefExpected) {
            throw ("pre-existing task-definition.xml differs from this install's generated definition; refusing to overwrite unknown content: " + $taskDefPath)
        }
        $cls = 'shared'
        if ($prevOwned.ContainsKey($taskDefRel)) { $cls = $prevOwned[$taskDefRel] }
        $plan += @{ file = $taskDefRel; sha256 = (Get-FileSha256 $taskDefPath); classification = $cls }
        Write-Log ("pre-existing identical task definition, " + $cls)
    } else {
        $plan += @{ file = $taskDefRel; sha256 = $expectedDefHash; classification = 'planned' }
    }
    # D4 (R46.7): brokerDir pre-state audit BEFORE any ACL change. A
    # pre-existing NON-EMPTY broker dir without a previous owned record is
    # a foreign namespace - refuse instead of silently re-ACLing it; the
    # original ACL output is recorded so a controlled recovery is possible.
    $dirPreState = @{ existed = (Test-Path -LiteralPath $brokerDir); nonEmpty = $false; aclOutput = @() }
    if ($dirPreState.existed) {
        $dirItems = @(Get-ChildItem -LiteralPath $brokerDir -Force)
        $dirPreState.nonEmpty = ($dirItems.Count -gt 0)
        if ($dirPreState.nonEmpty) {
            # R51.2: a non-empty pre-existing dir is foreign UNLESS its
            # content is exactly THIS install's expected namespace
            # (the 64-hex control key this component owns, its runtime
            # artifacts, or a previous ownership record).
            # R51.2 + contract §11.2: the allowed namespace covers the
            # broker's own runtime artifacts - including the PROTECTED
            # lifecycle.json / maintenance.json records the server writes
            # (each with its atomic .tmp form). A reinstall over a
            # deliberately stopped/maintenance-window broker must never
            # mistake those records for foreign content.
            $allowed = @('broker-control-key', 'broker-status.json',
                         'broker-status.json.tmp', 'install-state.json',
                         'broker-pre-acl.dat', 'task-definition.xml',
                         'lifecycle.json', 'lifecycle.json.tmp',
                         'maintenance.json', 'maintenance.json.tmp')
            foreach ($item in $dirItems) {
                if ($item.PSIsContainer -or $item.Name -notin $allowed) {
                    throw ("broker dir already exists with foreign content (" + $item.Name + "); refusing to re-ACL a shared directory")
                }
                if ($item.Name -eq 'broker-control-key') {
                    $existing = ([IO.File]::ReadAllText($item.FullName)).Trim()
                    if ($existing -notmatch '^[0-9a-f]{64}$') {
                        throw ("pre-existing broker-control-key is not a 64-hex value; refusing")
                    }
                }
            }
        }
        $aclProbe = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @('icacls', $brokerDir)
        if ($aclProbe.ExitCode -ne 0) {
            # R49-2: a FAILED original-ACL read refuses - installing over a
            # directory whose pre-change state cannot be recorded is not
            # recoverable.
            throw ("broker dir pre-state ACL could not be read (exit " + $aclProbe.ExitCode + "); refusing to change permissions without a recovery record")
        }
        $dirPreState.aclOutput = @($aclProbe.Output)
    }

    # Task ownership: an existing same-name task must reference THIS
    # install's wrapper (or be owned by the previous record) - an unknown
    # task is NEVER force-overwritten, and a query failure refuses (R44-5).
    $taskFacts = Get-BrokerTaskFacts -Cfg $Cfg -SchtasksRunner $SchtasksRunner `
        -WrapperPath $wrapperPath -PrevOwned $prevTaskOwned
    if ($null -eq $taskFacts.Exists) {
        throw ("install refused: task ownership could not be established (" + $taskFacts.Reason + ")")
    }
    if ($taskFacts.Exists -and -not $taskFacts.Owned) {
        throw ("same-name task '" + $Cfg['TaskName'] + "' exists but is not owned by this install (" + $taskFacts.Reason + "); refusing to overwrite")
    }

    # ---------------- Phase 0.4: reentry recovery (R53.2) ----------
    # A leftover broker-pre-acl.dat means a previous install saved
    # the ORIGINAL ACL and may have changed it before dying. The
    # pending recovery is CONSUMED here - never overwritten.
    # R55.4: reentry recovery is MANIFEST-DRIVEN: the acl_recovery state
    # in the manifest (not the physical backup file) determines what
    # to do. A pending/failed state with a MISSING backup refuses.
    # §57: ONE record contract (Get-AclRecoveryRecord) decides between a
    # supported legacy manifest (no acl_recovery property at all) and an
    # existing record; a record that exists with a missing/empty/illegal
    # state (or a pending/failed without a backup path) throws HERE and is
    # never overwritten by the Phase 0.5 rewrite.
    $recRecord = $null
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $prevState = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
        $recRecord = Get-AclRecoveryRecord -Manifest $prevState
        if ($null -ne $recRecord -and $recRecord.State -in 'pending', 'failed') {
            $prevBackup = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $recRecord.Backup
            if (-not (Test-Path -LiteralPath $prevBackup -PathType Leaf)) {
                throw ("pending ACL recovery (state=" + $recRecord.State +
                        ") but backup file is missing: " + $recRecord.Backup +
                        "; refusing to install over an unrecovered state")
            }
        }
    }
    $pendingBackup = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath 'broker-pre-acl.dat'
    if (Test-Path -LiteralPath $pendingBackup -PathType Leaf) {
        if ($null -ne $recRecord -and $recRecord.State -eq 'none') {
            # R56 deep-check: 'none' certifies the recovery was consumed or
            # intentionally closed (the terminal state is recorded BEFORE
            # the deletion), so a surviving file is crash GC between record
            # and delete - never unapplied recovery material. Remove and
            # proceed; no restore is ever attempted on it.
            Remove-Item -LiteralPath $pendingBackup -Force
            Write-Log "stale ACL backup with recovery state=none removed (crash GC between record and delete)"
        } elseif ($null -ne $recRecord -and $recRecord.State -in 'pending', 'failed') {
            Undo-AclRecoveryState -BrokerDir $brokerDir -Cfg $Cfg `
                -IcaclsRunner $IcaclsRunner -StatePath $statePath
            if (Test-Path -LiteralPath $pendingBackup -PathType Leaf) {
                throw ("pending ACL recovery failed (see warnings above); refusing to install until the original state is recovered")
            }
            Write-Log "reentry recovery completed; proceeding with a fresh install"
        } else {
            # No manifest, or a manifest WITHOUT an acl_recovery record:
            # the backup's provenance is unknown - never auto-applied.
            throw ("orphan ACL backup without an ownership record requires operator review: " + $pendingBackup)
        }
    }
    # ---------------- Phase 0.5: persist the manifest BEFORE any ACL
    # change (R51.3) - the original dir state is on disk before the
    # protection boundary is touched, so a failed ACL window still has
    # its recovery record.
    $manifest = @($plan)
    # The manifest dir must exist for the persistence itself - creating
    # it here is idempotent and is NOT an ACL change (Phase 1 does that
    # with its own /save-restore recovery).
    New-Item -ItemType Directory -Path $brokerDir -Force | Out-Null
    $manifest += @{ file = 'broker\broker-status.json'; sha256 = ''; classification = 'runtime' }
    Write-InstallState -Cfg $Cfg -Modules $manifest -StatePath $statePath -WrapperRel $wrapperRel -PreDir $dirPreState
    Write-Log "recovery manifest persisted BEFORE any ACL change (R51.3)"
    $aclBackupRel = ''

    # ---------------- Phase 1: protection boundary (first write) ----------
    try {
        New-Item -ItemType Directory -Path $brokerDir -Force | Out-Null
        if ($dirPreState.existed) {
            # R51.3: a PRE-EXISTING directory gets a REPLAYABLE ACL
            # backup (icacls /save) BEFORE the change - the recovery
            # is a controlled object operation, not just a log row.
            $aclBackupRel = 'broker-pre-acl.dat'
            $aclBackupPath = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $aclBackupRel
            if (Test-Path -LiteralPath $aclBackupPath -PathType Leaf) {
                throw ("a pending ACL backup already exists; refusing to overwrite the original recovery material")
            }
            $saveResult = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @(
                'icacls', $brokerDir, '/save', $aclBackupPath)
            if ($saveResult.ExitCode -ne 0) {
                throw ("broker dir pre-change ACL backup failed (exit " + $saveResult.ExitCode + "); refusing to change permissions without a recovery record")
            }
            Set-AclRecoveryState -StatePath $statePath -State 'pending' -BackupRel $aclBackupRel
            Write-Log "pre-change ACL backup saved; recovery state PENDING (icacls /save)"
        }
        if (-not (Set-BrokerDirAcl -BrokerDir $brokerDir -OperatorAccount $Cfg['OperatorAccount'] -IcaclsRunner $IcaclsRunner)) {
            throw "broker dir ACL could not be set (icacls failed); refusing to create the control secret"
        }
        $verdict = Test-BrokerDirAcl -BrokerDir $brokerDir -OperatorAccount $Cfg['OperatorAccount'] -IcaclsRunner $IcaclsRunner
        if (-not $verdict.Ok) {
            throw ("broker dir ACL verification failed: " + $verdict.Reason + "; refusing to write the control secret")
        }
        Write-Log "broker dir ACL verified (SYSTEM/admin full, operator read-only, um_share none)"
    } catch {
        # Nothing owned exists yet. An EMPTY, SELF-CREATED broker dir is
        # removed so the failure leaves no unrecorded namespace; a
        # PRE-EXISTING directory is NEVER treated as self-created - it
        # stays (its pre-change ACL rows were audited into the manifest
        # intent for a controlled recovery).
        try {
            Undo-AclRecoveryState -BrokerDir $brokerDir -Cfg $Cfg `
                -IcaclsRunner $IcaclsRunner -StatePath $statePath
            if ((-not $dirPreState.existed) -and (Test-Path -LiteralPath $brokerDir)) {
                $left = @(Get-ChildItem -LiteralPath $brokerDir -Force)
                if ($left.Count -eq 0) { Remove-Item -LiteralPath $brokerDir -Force }
            }
        } catch {
            Write-Log ("WARNING: recovery attempt failed: " + $_.Exception.Message)
        }
        throw
    }

    # ---------------- Phase 2: persist the recovery manifest FIRST --------
    $aclBackupRel = ''
    $auditText = ("broker dir pre-state audited: existed=" +
                  $dirPreState.existed +
                  " nonEmpty=" + $dirPreState.nonEmpty +
                  " aclRows=" + @($dirPreState.aclOutput).Count)
    Write-Log $auditText

    # ---------------- Phase 3: execute ------------------------------------
    try {
        foreach ($m in $manifest) {
            if ($m.classification -ne 'planned') { continue }   # owned/shared exist already
            if ($m.file -eq 'broker\broker-status.json') { continue }   # runtime artifact
            $dst = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $m.file
            if ($m.file -eq 'broker\broker-control-key') {
                # R51.2: the CREATION FACT decides the upgrade - a
                # kept pre-existing key is demoted to 'shared'
                # (never adopted by hash); only a NEWLY generated key
                # becomes created.
                $created = Write-BrokerKeyFile -Path $dst -Label 'broker-control-key'
                if ($created) {
                    Set-ManagedEntryState -StatePath $statePath -File $m.file `
                        -Classification 'created' -Sha256 (Get-FileSha256 $dst)
                } else {
                    Set-ManagedEntryState -StatePath $statePath -File $m.file `
                        -Classification 'shared' -Sha256 ''
                    Write-Log ("key appeared after inventory; kept as shared (not adopted): " + $m.file)
                }
                continue
            }
            if ($m.file -eq 'instance-channel-key') {
                $created = Write-BrokerKeyFile -Path $dst -Label 'instance-channel-key'
                if ($created) {
                    Set-ManagedEntryState -StatePath $statePath -File $m.file `
                        -Classification 'created' -Sha256 (Get-FileSha256 $dst)
                } else {
                    Set-ManagedEntryState -StatePath $statePath -File $m.file `
                        -Classification 'shared' -Sha256 ''
                    Write-Log ("key appeared after inventory; kept as shared (not adopted): " + $m.file)
                }
                continue
            }
            if ($m.file -eq 'run-broker.cmd') {
                # R51.2: a pre-existing identical wrapper stays in its
                # Phase 0 classification (shared/created); only a
                # FRESH write of THIS run becomes created.
                if (Test-Path -LiteralPath $wrapperPath -PathType Leaf) {
                    $actual = Get-FileSha256 $wrapperPath
                    if ($m.sha256 -and $actual -ne $m.sha256) { throw ("wrapper hash mismatch for " + $dst) }
                    Write-Log "pre-existing identical wrapper kept (" + $m.classification + ")"
                    continue
                }
                [IO.File]::WriteAllText($wrapperPath, $wrapperContent, (New-Object System.Text.UTF8Encoding($false)))
                $actual = Get-FileSha256 $wrapperPath
                Set-ManagedEntryState -StatePath $statePath -File $m.file `
                    -Classification 'created' -Sha256 $actual
                Write-Log "run-broker.cmd generated (self-directory relative paths)"
                continue
            }
            if ($m.file -eq 'broker\task-definition.xml') {
                # R62-D: the definition document has NO source copy - it is
                # WRITTEN in the dedicated Phase 3 section (Unicode, verified
                # against the inventoried hash) and consumed by the XML-based
                # /Create. The copy loop must never treat it as a source copy.
                continue
            }
            $src = $srcByFile[$m.file]
            $hash = Copy-VerifiedModule -Source $src -Destination $dst
            if ($hash -ne $m.sha256) { throw ("copied hash differs from the inventoried expectation for " + $m.file) }
            # R51.2: created = THIS run verified the copy; planned
            # entries with only an EXPECTED hash never upgrade here.
            Set-ManagedEntryState -StatePath $statePath -File $m.file `
                -Classification 'created' -Sha256 $hash
        }
        # R62-D: the task definition document is written (Unicode, matching
        # the inventoried hash) BEFORE registration, then consumed by the
        # XML-based /Create. A pre-existing identical definition keeps its
        # Phase 0 classification (shared/prev-owned) - only a FRESH write of
        # THIS run becomes created (same rule as the wrapper, R51.2).
        $taskDefRel = 'broker\task-definition.xml'
        $taskDefDst = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $taskDefRel
        $expectedEntry = @($manifest | Where-Object { $_.file -eq $taskDefRel }) | Select-Object -First 1
        if (Test-Path -LiteralPath $taskDefDst -PathType Leaf) {
            $actualDefHash = Get-FileSha256 $taskDefDst
            if ($expectedEntry.sha256 -and $actualDefHash -ne $expectedEntry.sha256) {
                throw ("task definition hash mismatch for " + $taskDefDst)
            }
            Write-Log ("pre-existing identical task definition kept (" + $expectedEntry.classification + ")")
        } else {
            [IO.File]::WriteAllText($taskDefDst,
                (New-BrokerTaskDefinitionXml -Cfg $Cfg -WrapperPath $wrapperPath),
                (New-Object System.Text.UnicodeEncoding($false, $true)))
            $actualDefHash = Get-FileSha256 $taskDefDst
            if ($actualDefHash -ne $expectedEntry.sha256) {
                throw ("task definition hash mismatch for " + $taskDefDst)
            }
            Set-ManagedEntryState -StatePath $statePath -File $taskDefRel `
                -Classification 'created' -Sha256 $actualDefHash
            Write-Log "task definition written+verified (no auto trigger for ONDEMAND; BootTrigger for ONSTART)"
        }
        # R49-4: the registration itself re-checks ownership against
        # the CURRENT task definition right before creating.
        Register-BrokerTask -Cfg $Cfg -WrapperPath $wrapperPath `
            -SchtasksRunner $SchtasksRunner -PrevOwned $prevTaskOwned

        # Projection landing ACL: checked (exit code verified) but advisory -
        # the broker runtime ACL checker refuses at publish time anyway.
        $projection = Join-Path $Cfg['RunRoot'] 'um-share-projection.json'
        if (Test-Path -LiteralPath $projection -PathType Leaf) {
            $aclResult = Invoke-CommandRunner -Runner $IcaclsRunner -ArgumentList @('icacls', $projection)
            if ($aclResult.ExitCode -ne 0) {
                Write-Log ("WARNING: projection icacls failed (exit " + $aclResult.ExitCode + "); the runtime ACL checker will decide")
            } else {
                Write-Log "projection landing ACL queried (runtime checker re-verifies at publish)"
            }
        }
        # R49-2: hashes are recorded IN PLACE at every write point
        # (Set-ManagedEntryState) - no end-of-install backfill window
        # exists any more.
        # R53.2: the install COMPLETED - the intended ACL is in place,
        # the recovery material is consumed (controlled cleanup with
        # bookkeeping), and the manifest state advances to none.
        # R55.4: record the terminal state FIRST, then delete the backup
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            Set-AclRecoveryState -StatePath $statePath -State 'none' -BackupRel ''
        }
        $doneBackup = Join-Path $Cfg['RunRoot'] 'broker-pre-acl.dat'
        if (Test-Path -LiteralPath $doneBackup -PathType Leaf) {
            Remove-Item -LiteralPath $doneBackup -Force
        }
        Write-Log 'broker install complete (task NOT started; start is D4/L2 authorized only)'
    } catch {
        # The recovery manifest was persisted BEFORE any content write, so
        # every failure window already has complete ownership records
        # (R44-5: no window without a manifest, no directory dependence).
        try {
            Undo-AclRecoveryState -BrokerDir $brokerDir -Cfg $Cfg `
                -IcaclsRunner $IcaclsRunner -StatePath $statePath
        } catch {
            Write-Log ("WARNING: recovery attempt failed: " + $_.Exception.Message)
        }
        Write-Log ("install failed; recovery manifest retained for uninstall: " + $_.Exception.Message)
        throw
    }
}

function Uninstall-Broker {
    param([hashtable]$Cfg, [scriptblock]$SchtasksRunner, [scriptblock]$IcaclsRunner)
    $brokerDir = Join-Path $Cfg['RunRoot'] 'broker'
    $statePath = Join-Path $brokerDir 'install-state.json'

    # Gate 0: the manifest comes FIRST - no task operation and no file
    # removal without ownership records (R44-6).
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        Write-Log 'install-state.json absent: NO task operation, NO files removed (ownership unknown)'
        foreach ($known in @('run-broker.cmd', 'instance-channel-key',
                             (Join-Path $brokerDir 'broker-control-key'))) {
            if (Test-Path -LiteralPath $known -PathType Leaf) {
                Write-Log ("  broker artifact present (not owned, not removed): " + $known)
            }
        }
        return
    }
    try {
        $state = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    } catch {
        throw ("uninstall aborted: install-state.json unreadable; nothing removed (" + $_.Exception.Message + ")")
    }
    if ([string]$state.task_name -ne $Cfg['TaskName']) {
        throw ("uninstall aborted: configured task '" + $Cfg['TaskName'] +
               "' does not match the manifest task '" + $state.task_name + "'")
    }
    $wrapperRel = [string]$state.task_wrapper
    if (-not $wrapperRel) { $wrapperRel = 'run-broker.cmd' }
    $wrapperPath = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $wrapperRel

    # Gate 0.5 (D2): validate EVERY manifest-owned path BEFORE any task
    # action - a bad path at the tail of the manifest must refuse before
    # End/Delete ever happens.
    foreach ($m in @($state.modules)) {
        if ($m.classification -notin 'owned', 'created', 'planned') { continue }
        [void](Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $m.file)
    }

    # Gate 1: the task identity is bound to THIS manifest (its definition
    # must reference this install's wrapper); a query failure refuses.
    $taskFacts = Get-BrokerTaskFacts -Cfg $Cfg -SchtasksRunner $SchtasksRunner `
        -WrapperPath $wrapperPath -PrevOwned $true
    if ($null -eq $taskFacts.Exists) {
        throw ("uninstall aborted: task identity could not be verified (" + $taskFacts.Reason + "); no files removed")
    }
    if ($taskFacts.Exists -and -not $taskFacts.Owned) {
        throw ("uninstall aborted: task '" + $Cfg['TaskName'] + "' exists but references another definition (" + $taskFacts.Reason + "); no files removed")
    }
    $stop = Confirm-TaskStopped -Cfg $Cfg -SchtasksRunner $SchtasksRunner
    if (-not $stop.Ok) {
        throw ("uninstall aborted: broker task stop not confirmed (" + $stop.Reason + "); no files removed")
    }
    if ($taskFacts.Exists) {
        $del = Remove-BrokerTask -Cfg $Cfg -SchtasksRunner $SchtasksRunner
        if (-not $del.Ok) {
            throw ("uninstall aborted: broker task delete not confirmed (" + $del.Reason + "); no files removed")
        }
        Write-Log ("broker task removed (" + $del.Reason + ")")
    } else {
        Write-Log "broker task already absent (idempotent)"
    }

    # Gate 1.5 (R53.2): a pending/failed ACL recovery is consumed FIRST
    # (the pre-change ACL is restored); a failed restore BLOCKS the
    # manifest deletion below.
    Undo-AclRecoveryState -BrokerDir $brokerDir -Cfg $Cfg `
        -IcaclsRunner $IcaclsRunner -StatePath $statePath
    $state = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    if ([string]$state.acl_recovery.state -eq 'failed') {
        throw ("uninstall aborted: ACL recovery FAILED earlier (backup kept: " +
               [string]$state.acl_recovery.backup + "); operator review required")
    }
    # R56 deep-check: a state=none recovery certifies consumed/closed - a
    # surviving backup file is crash GC between record and delete; close it
    # so uninstall leaves no stale recovery material at the run root.
    if ([string]$state.acl_recovery.state -eq 'none') {
        $staleBackup = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath 'broker-pre-acl.dat'
        if (Test-Path -LiteralPath $staleBackup -PathType Leaf) {
            Remove-Item -LiteralPath $staleBackup -Force
            Write-Log "stale ACL backup (recovery state=none) removed as GC"
        }
    }
    $state = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json

    # Gate 2: delete ONLY manifest-owned entries through the whitelist +
    # reparse guard. An owned CODE entry whose content no longer matches
    # the recorded hash was modified after this install (another task's
    # change) - it is KEPT and COUNTED so the ownership record survives
    # for traceability (R44-6).
    $kept = 0
    foreach ($m in @($state.modules)) {
        if ($m.classification -notin 'owned', 'created', 'planned') {
            Write-Log ("kept " + $m.classification + ": " + $m.file)
            continue
        }
        if ($m.classification -eq 'planned') {
            # R51.2: planned is an INTENT, never delete permission -
            # an expected hash does not prove THIS install created it.
            $kept++
            Write-Log ("WARNING: planned (never recorded as created), KEPT: " + $m.file)
            continue
        }
        $dst = Get-ManagedPath -TargetRoot $Cfg['RunRoot'] -RelPath $m.file
        if (-not (Test-Path -LiteralPath $dst -PathType Leaf)) {
            Write-Log ("owned entry already absent: " + $m.file)
            continue
        }
        if (-not $m.sha256) {
            # R49-2: an EMPTY hash (planned/never recorded) is NOT
            # delete permission - the object may be someone else's
            # replacement; keep it and keep the record for review.
            $kept++
            Write-Log ("WARNING: entry without recorded content hash, KEPT: " + $m.file)
            continue
        }
        $current = Get-FileSha256 $dst
        if ($current -ne $m.sha256) {
            $kept++
            Write-Log ("WARNING: owned file changed after install, KEPT: " + $m.file)
            continue
        }
        Remove-Item -LiteralPath $dst -Force
        if (Test-Path -LiteralPath $dst) {
            throw ("uninstall aborted: failed to remove " + $dst + "; remaining manifest kept for retry")
        }
        Write-Log ("removed owned file " + $m.file)
    }

    # Gate 3: runtime artifacts first; the ownership record is closed ONLY
    # when nothing was kept; the dir is removed ONLY when verifiably empty.
    foreach ($runtime in @('broker-status.json', 'broker-status.json.tmp')) {
        $p = Join-Path $brokerDir $runtime
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            Remove-Item -LiteralPath $p -Force
            Write-Log ("removed broker runtime artifact " + $runtime)
        }
    }
    if ($kept -gt 0) {
        Write-Log ("install-state.json KEPT (" + $kept + " owned entries were modified after install); broker dir kept for traceability")
        return
    }
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        Remove-Item -LiteralPath $statePath -Force
        Write-Log 'install-state.json removed (ownership record closed)'
    }
    if (Test-Path -LiteralPath $brokerDir) {
        $leftovers = @(Get-ChildItem -LiteralPath $brokerDir -Force)
        if ($leftovers.Count -eq 0) {
            Remove-Item -LiteralPath $brokerDir -Force
            Write-Log 'broker dir removed (was empty)'
        } else {
            $names = (@($leftovers) | ForEach-Object { $_.Name }) -join ', '
            Write-Log ("WARNING: broker dir NOT empty (" + $names + "); directory kept")
        }
    }
    Write-Log 'broker uninstall complete; share deployment files untouched'
}
