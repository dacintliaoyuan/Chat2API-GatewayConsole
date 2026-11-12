# Offline tests for install-share-broker-lib.ps1 (R41-5/R41-6/R44).
# Everything runs on a temp RunRoot with FAKE schtasks/icacls runners - no
# admin rights, no real task scheduler, no real ACL modification, no real
# uninstall. R44 coverage: zero-side-effect inventory (absent-then-conflict
# must not copy), task ownership binding (unknown same-name task refuses,
# query failure refuses), manifest-first uninstall (no manifest -> zero task
# operations), query-failure != absent, deny-override ACL verdicts
# (path-prefix ACE never dropped, DENY never counts as a grant, unknown
# codes refuse), reparse-point guard, kept-modified traceability and
# idempotent repeat uninstall.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'um-share-deploy\install-share-broker-lib.ps1')

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

function New-FakeSchtasks {
    # Configurable fake: records calls; /Query /XML answers ownership,
    # /Query CSV answers the task state; absence is ONLY the explicit
    # "cannot find" evidence; a query failure is a distinct state.
    # ForeignOnCreate: the SECOND /XML query reports a FOREIGN task
    # definition - the R49-4 race window between inventory and register.
    param([bool]$CreateOk = $true, [bool]$DeleteOk = $true,
          [string]$QueryState = 'Ready', [string]$OwnedWrapper = '',
          [bool]$QueryFailed = $false, [string]$BrokerPidsJson = '',
          [bool]$ForeignOnCreate = $false, [bool]$TaskAbsent = $false,
          [string]$TaskArguments = '',
          [string]$TaskPrincipal = 'S-1-5-18',
          [bool]$DualExec = $false,
          [string[]]$ChangeAcl = $null,
          [bool]$ComHandler = $false,
          [int]$PrincipalCount = 1)
    $fakeState = @{
        Calls = New-Object System.Collections.Generic.List[string]
        CreateOk = $CreateOk
        DeleteOk = $DeleteOk
        QueryState = $QueryState
        OwnedWrapper = $OwnedWrapper
        QueryFailed = $QueryFailed
        BrokerPidsJson = $BrokerPidsJson
        ForeignOnCreate = $ForeignOnCreate
        TaskArguments = $TaskArguments
        TaskPrincipal = $TaskPrincipal
        DualExec = $DualExec
        ChangeAcl = $ChangeAcl
        ComHandler = $ComHandler
        PrincipalCount = $PrincipalCount
        XmlQueries = 0
        TaskAbsent = $TaskAbsent
        Deleted = $false
    }
    return @{
        State = $fakeState
        Runner = {
            param([string[]]$argv)
            $s = $fakeState
            $action = ($argv | Select-Object -Skip 1 | Select-Object -First 1)
            $s['Calls'].Add(($argv -join ' '))
            if ($action -eq '/Create') {
                if (-not $s['CreateOk']) { return (New-TestRunResult -ExitCode 1 -Output @('create failed')) }
                return (New-TestRunResult -ExitCode 0 -Output @('SUCCESS'))
            }
            if ($action -eq '/End') {
                return (New-TestRunResult -ExitCode 0 -Output @('ended'))
            }
            if ($action -eq '/Delete') {
                if (-not $s['DeleteOk']) { return (New-TestRunResult -ExitCode 1 -Output @('delete failed')) }
                $s['Deleted'] = $true
                return (New-TestRunResult -ExitCode 0 -Output @('deleted'))
            }
            if ($argv[0] -in @('powershell', 'pwsh')) {
                return (New-TestRunResult -ExitCode 0 -Output @($s['BrokerPidsJson']))
            }
            if ($action -eq '/Query') {
                if ($s['TaskAbsent']) {
                    return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
                }
                if ($s['QueryFailed']) {
                    return (New-TestRunResult -ExitCode 1 -Output @('ERROR: Access is denied.'))
                }
                if ($s['Deleted']) {
                    return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
                }
                if ($argv -contains '/XML') {
                    if ($s['TaskAbsent']) {
                        return (New-TestRunResult -ExitCode 1 -Output @('ERROR: The system cannot find the file specified.'))
                    }
                    $tr = $s['OwnedWrapper']
                    if (-not $tr) { $tr = 'C:\somewhere-else\other.cmd' }
                    if ($s['ForeignOnCreate']) {
                        # R49-4 race window: the SECOND ownership query
                        # reports a foreign definition (inventory said
                        # ours, registration must re-check).
                        $s['XmlQueries'] = 1 + [int]$s['XmlQueries']
                        if ([int]$s['XmlQueries'] -ge 2) {
                            $s['ForeignOnCreate'] = $false
                            $tr = 'C:\foreign-race\other.cmd'
                        }
                    }
                    $xmlHead = '<?xml version="1.0" encoding="UTF-16"?>'
                    $xmlBody = ('<Task version="1.2">' +
                                '<Principals><Principal id="Author">' +
                                '<UserId>' + $s['TaskPrincipal'] + '</UserId>' +
                                '</Principal>')
                    if ([int]$s['PrincipalCount'] -ge 2) {
                        # R53.4: a second run identity is a foreign definition.
                        $xmlBody += '<Principal id="Second"><UserId>S-1-5-32-544</UserId></Principal>'
                    }
                    $xmlBody += ('</Principals>' +
                                 '<Actions Context="Author"><Exec>' +
                                 '<Command>' + $tr + '</Command>' +
                                 '<Arguments>' + $s['TaskArguments'] + '</Arguments>' +
                                 '</Exec>')
                    if ($s['DualExec']) {
                        $xmlBody += ('<Exec><Command>C:\other.exe</Command>' +
                                     '<Arguments>extra</Arguments></Exec>')
                    }
                    if ($s['ComHandler']) {
                        # R53.4: a non-Exec action sibling is part of the
                        # Actions whitelist - must refuse, not just count Exec.
                        $xmlBody += '<ComHandler><ClassId>{12345678-1234-1234-1234-123456789012}</ClassId></ComHandler>'
                    }
                    $xmlBody += '</Actions></Task>'
                    $xml = $xmlHead + $xmlBody
                    return (New-TestRunResult -ExitCode 0 -Output @($xml))
                }
                return (New-TestRunResult -ExitCode 0 -Output @('"t","2026-09-08","' + $s['QueryState'] + '"'))
            }
            return (New-TestRunResult -ExitCode 1 -Output @('unexpected'))
        }.GetNewClosure()
    }
}

function New-SimulatedAclRunner {
    # R53.2 minimal recovery fixture: the fake maintains a SIMULATED ACL
    # state; icacls /save really writes an isolated backup file (content =
    # the simulated ACL), /restore reads the backup and rolls the simulated
    # state back. Query returns the simulated state. This is the state
    # machine the recovery contract needs - not a fixed exit-0 stub.
    # $script:SimAcl holds the CURRENT simulated ACL rows.
    param([string[]]$InitialAcl, [string[]]$ChangeAcl = $null)
    $fakeState = @{
        Calls = New-Object System.Collections.Generic.List[string]
        SimAcl = @($InitialAcl)
        ChangeAcl = $ChangeAcl
        RestoreExitCode = 0          # injectable failure for the negative
        SaveExitCode = 0
    }
    return @{
        State = $fakeState
        Runner = {
            param([string[]]$argv)
            $s = $fakeState
            $joined = ($argv -join ' ')
            $s['Calls'].Add($joined)
            if ($joined -match '/save') {
                if ($s['SaveExitCode'] -ne 0) {
                    return (New-TestRunResult -ExitCode $s['SaveExitCode'] -Output @('save failed'))
                }
                # Real isolated backup: the file holds the simulated ACL.
                [IO.File]::WriteAllLines($argv[-1], @('SIMULATED-ACL:') + @($s['SimAcl']))
                return (New-TestRunResult -ExitCode 0 -Output @('saved'))
            }
            if ($joined -match '/restore') {
                if ($s['RestoreExitCode'] -ne 0) {
                    return (New-TestRunResult -ExitCode $s['RestoreExitCode'] -Output @('restore failed'))
                }
                if (-not (Test-Path -LiteralPath $argv[-1] -PathType Leaf)) {
                    return (New-TestRunResult -ExitCode 1 -Output @('backup missing'))
                }
                $rows = @(Get-Content -LiteralPath $argv[-1] | Where-Object { $_ -notmatch '^SIMULATED-ACL:' })
                if ($rows.Count -eq 0) {
                    return (New-TestRunResult -ExitCode 1 -Output @('backup unreadable'))
                }
                $s['SimAcl'] = $rows
                return (New-TestRunResult -ExitCode 0 -Output @('restored'))
            }
            if ($joined -match '/inheritance:r') {
                # The change: default introduces um_share (verdict
                # failure negatives); ChangeAcl injectable for the
                # R53.2 reentry cases that must COMPLETE.
                $s['SimAcl'] = if ($s['ChangeAcl']) { @($s['ChangeAcl']) } else {
                    @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                      'BUILTIN\Administrators:(OI)(CI)(F)',
                      'MACHINE\dacint:(OI)(CI)(RX)',
                      'DACINT\um_share:(OI)(RX)') }
                return (New-TestRunResult -ExitCode 0 -Output @('acl changed'))
            }
            return (New-TestRunResult -ExitCode 0 -Output (@($argv[1]) + @($s['SimAcl'])))
        }.GetNewClosure()
    }
}

function New-GoodIcaclsRunner {
    # Canned broker-dir ACL verdict: SYSTEM/admin full, operator RX-only.
    # R53.2: /save REALLY writes the isolated backup file (content = the
    # canned ACL) and /restore consumes it, so the recovery state
    # machine is exercised end-to-end without a real host.
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

function New-FailIcaclsRunner {
    return {
        param([string[]]$argv)
        New-TestRunResult -ExitCode 2 -Output @('access denied')
    }.GetNewClosure()
}

function New-SequenceIcaclsRunner {
    # Sequence fake for the R51.3 failure-window probe: call 1 = the
    # ORIGINAL-state probe (good ACL), call 2 = the ACL CHANGE (ok),
    # call 3+ = verification (BROKEN output -> verdict fails). /Restore
    # invocations are recorded for the rollback assertion. This is the
    # exact sequence of a real install whose ACL change succeeds but
    # whose verification fails.
    $fakeState = @{
        Calls = New-Object System.Collections.Generic.List[string]
        IcaclsCalls = 0
    }
    return @{
        State = $fakeState
        Runner = {
            param([string[]]$argv)
            $s = $fakeState
            $joined = ($argv -join ' ')
            $s['Calls'].Add($joined)
            if ($joined -match '/Restore') {
                return (New-TestRunResult -ExitCode 0 -Output @('restored'))
            }
            if ($joined -match '/save') {
                # R53.2: /save really writes the isolated backup file
                # (so Undo's backup-existence check finds it).
                [IO.File]::WriteAllLines($argv[-1], @('SIMULATED-ACL:',
                    'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                    'BUILTIN\Administrators:(OI)(CI)(F)',
                    'MACHINE\dacint:(OI)(CI)(RX)'))
                return (New-TestRunResult -ExitCode 0 -Output @('saved'))
            }
            if ($joined -match '/inheritance:r') {
                $s['IcaclsCalls']++
                return (New-TestRunResult -ExitCode 0 -Output @('acl changed'))
            }
            # probe / verification query
            $s['IcaclsCalls']++
            if ($s['IcaclsCalls'] -eq 1) {
                return (New-TestRunResult -ExitCode 0 -Output @(
                    $argv[1], 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                    'BUILTIN\Administrators:(OI)(CI)(F)',
                    'MACHINE\dacint:(OI)(CI)(RX)'))
            }
            # verification AFTER the change: um_share present -> verdict fails
            return (New-TestRunResult -ExitCode 0 -Output @(
                $argv[1], 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                'BUILTIN\Administrators:(OI)(CI)(F)',
                'MACHINE\dacint:(OI)(CI)(RX)', 'DACINT\um_share:(OI)(RX)'))
        }.GetNewClosure()
    }
}

function New-TestSourceDir {
    # Copy the real reviewed module closure into a temp source dir (read-only
    # use of the repo files).
    param([string]$Dir)
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    foreach ($m in (Get-BrokerSourceManifest -SourceRoot $root)) {
        Copy-Item -LiteralPath $m.Src -Destination (Join-Path $Dir (Split-Path $m.Src -Leaf)) -Force
    }
}

function New-Cfg {
    param([string]$RunRoot, [string]$SourceRoot)
    return (New-BrokerConfig -RunRoot $RunRoot -SourceRoot $SourceRoot `
        -TaskName 'umshare-broker-test' -ShareTaskName 'umshare-live-test' `
        -BrokerPipeName '\\.\pipe\um-share-broker-test' `
        -InstancePipePrefix '\\.\pipe\um-share-instance-test-' `
        -OperatorAccount 'dacint' -InstanceOwnerSid '' -BrokerPipeSddl '' `
        -PythonHome 'C:\PythonTest')
}

function New-RunRoot {
    param([string]$Dir)
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $Dir 'um-share-python.exe') -Value 'dummy'
    # P3 (D4-P): the install prerequisite is the deployment-mode FACT (the
    # same explicit file the guard requires) - guard-status.json is a
    # per-run guard TRANSIENT and is no longer part of the install gate.
    Set-Content -LiteralPath (Join-Path $Dir 'deploy-mode.json') -Value '{"mode": "legacy"}'
}

function Count-Owned {
    param([object]$StateJson)
    return @(@($StateJson.modules) | Where-Object { $_.classification -eq 'owned' }).Count
}

# ------------------------------------------------------------------ tests

Write-Host '== path whitelist + reparse guard (R44-6) =='
$wd = Join-Path $env:TEMP ('isb-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $wd -Force | Out-Null
Assert-Equal (Get-ManagedPath -TargetRoot $wd -RelPath 'broker\broker-control-key') (Join-Path $wd 'broker\broker-control-key') 'subdir entry resolves under root'
Assert-Throws { Get-ManagedPath -TargetRoot $wd -RelPath '..\evil.txt' } 'traversal rejected' 'whitelist'
Assert-Throws { Get-ManagedPath -TargetRoot $wd -RelPath 'C:\abs\evil.txt' } 'absolute rejected' 'whitelist'
Assert-Throws { Get-ManagedPath -TargetRoot $wd -RelPath 'a:b.txt' } 'colon rejected' 'whitelist'
$junctionRoot = Join-Path $wd 'junction-run'
New-Item -ItemType Directory -Path (Join-Path $junctionRoot 'real') -Force | Out-Null
$escaped = Join-Path $env:TEMP ('isb-escape-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $escaped -Force | Out-Null
New-Item -ItemType Junction -Path (Join-Path $junctionRoot 'broker') -Target $escaped | Out-Null
Assert-Throws { Get-ManagedPath -TargetRoot $junctionRoot -RelPath 'broker\key' } 'reparse point inside root refuses' 'reparse point'

Write-Host '== Copy-VerifiedModule single-value return (R41-6) =='
$srcDir = Join-Path $wd 'src'
New-TestSourceDir -Dir $srcDir
$dstDir = Join-Path $wd 'dst'
New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
$hash = Copy-VerifiedModule -Source (Join-Path $srcDir 'share/runtime.py') -Destination (Join-Path $dstDir 'share/runtime.py')
Assert-True ($hash -is [string] -and $hash.Length -eq 64) 'return value is ONE sha256 string'
Assert-Throws { Copy-VerifiedModule -Source (Join-Path $srcDir 'absent.py') -Destination (Join-Path $dstDir 'a.py') } 'missing source throws' 'missing'

Write-Host '== generated wrapper namespace isolation + share port (R41-6/R44) =='
$expCfg = New-BrokerConfig -RunRoot 'D:\ExpRoot' -SourceRoot 'ignored' -TaskName 'umshare-broker-exp' -ShareTaskName 'umshare-live-exp' -BrokerPipeName '\\.\pipe\um-share-broker-exp' -InstancePipePrefix '\\.\pipe\um-share-instance-exp-' -OperatorAccount 'dacint' -InstanceOwnerSid '' -BrokerPipeSddl '' -PythonHome 'C:\PythonTest' -SharePort 9100
$wrapper = New-BrokerWrapperContent -Cfg $expCfg
Assert-True ($wrapper -notmatch 'C:\\ProgramData\\UMShareRun') 'wrapper has no production RunRoot reference'
Assert-True ($wrapper -notmatch 'pipe\\um-share-broker"') 'wrapper has no production broker pipe'
Assert-True ($wrapper -match '%~dp0um-share-python.exe') 'wrapper exe is self-directory relative'
Assert-True ($wrapper -match [regex]::Escape('--pipe-name "\\.\pipe\um-share-broker-exp"')) 'wrapper carries the experiment broker pipe'
Assert-True ($wrapper -match '--share-port 9100') 'wrapper carries the experiment share port'
Assert-True ($wrapper -notmatch '--sddl') 'wrapper omits sddl when uncalibrated'
Assert-True ($wrapper -notmatch '--expect-owner-sid') 'wrapper omits owner sid when uncalibrated'

Write-Host '== wrapper carries the read-only host-evidence wiring (contract §11.2) =='
$wrapArgv = Get-BrokerWrapperServerArgv -WrapperContent $wrapper -TargetRoot 'D:\ExpRoot'
$watchIdx = @($wrapArgv).IndexOf('--watchdog-script')
Assert-True ($watchIdx -ge 0) 'wrapper passes --watchdog-script'
Assert-Equal $wrapArgv[$watchIdx + 1] 'D:\ExpRoot\watchdog-umshare.ps1' 'watchdog script is install-relative'
$rrIdx = @($wrapArgv).IndexOf('--run-root')
Assert-True ($rrIdx -ge 0) 'wrapper passes --run-root'
Assert-Equal $wrapArgv[$rrIdx + 1] 'D:\ExpRoot\' 'run root resolves to the install root (trailing separator kept)'
$fwIdx = @($wrapArgv).IndexOf('--firewall-rule')
Assert-True ($fwIdx -ge 0) 'wrapper passes --firewall-rule'
Assert-Equal $wrapArgv[$fwIdx + 1] 'um-share-outbound-block' 'firewall rule name carried'
$sfIdx = @($wrapArgv).IndexOf('--share-faults-file')
Assert-True ($sfIdx -ge 0) 'wrapper passes --share-faults-file'
Assert-Equal $wrapArgv[$sfIdx + 1] 'D:\ExpRoot\run\live\share-faults.jsonl' 'share faults file is install-relative'
$ddIdx = @($wrapArgv).IndexOf('--dispatch-deadline')
Assert-True ($ddIdx -ge 0) 'wrapper passes --dispatch-deadline'
Assert-Equal $wrapArgv[$ddIdx + 1] '130' 'dispatch deadline bounded to 130'
Assert-Equal @($wrapArgv | Where-Object { $_ -eq '--run-root' }).Count 1 'run root appears EXACTLY once (no swallowed tail)'

Write-Host '== deny-override ACL verdict (R41-5/R44-4) =='
$brokerDirPath = 'C:\x\broker'
$goodAcl = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)')
$v = Test-AclOutput -Output $goodAcl -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True $v.Ok 'good broker dir ACL passes'
$withShare = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)', 'DACINT\um_share:(OI)(RX)')
$v = Test-AclOutput -Output $withShare -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'um_share presence refuses'
$dangerFirst = @(($brokerDirPath + ' DACINT\um_share:(I)(W)'), 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)')
$v = Test-AclOutput -Output $dangerFirst -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'dangerous ACE behind the path prefix on line 1 is CHECKED and refuses'
$denyOnly = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(DENY)(W)')
$v = Test-AclOutput -Output $denyOnly -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'DENY-only expected principal is NOT a grant (refused)'
$denyPlusAllow = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(DENY)(W)', 'MACHINE\dacint:(OI)(CI)(RX)')
$v = Test-AclOutput -Output $denyPlusAllow -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'specific DENY code is an unsupported combination (R49-5)'
$denyFullClears = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)', 'MACHINE\dacint:(DENY)(F)')
$v = Test-AclOutput -Output $denyFullClears -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'aggregated DENY F clears the grant (missing read)'
$denyReadKill = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)', 'MACHINE\dacint:(DENY)(RX)')
$v = Test-AclOutput -Output $denyReadKill -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'DENY of the required read code refuses'
$unknownCode = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(ZZ)')
$v = Test-AclOutput -Output $unknownCode -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'unknown permission code refuses (never silently safe)'
$stray = @($brokerDirPath, 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)', 'BUILTIN\Administrators:(OI)(CI)(F)', 'MACHINE\dacint:(OI)(CI)(RX)', 'Evil:(OI)(F)')
$v = Test-AclOutput -Output $stray -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @('um_share')
Assert-True (-not $v.Ok) 'stray write principal refuses'
$v = Test-AclOutput -Output @('garbage line') -TargetPath $brokerDirPath -AllowedFull @('SYSTEM') -ReadOnlyIdentity '' -Forbidden @()
Assert-True (-not $v.Ok) 'unparsable output refuses'
$v = Test-AclOutput -Output $goodAcl -TargetPath $brokerDirPath -AllowedFull @('SYSTEM', 'Administrators') -ReadOnlyIdentity 'dacint' -Forbidden @()
Assert-True $v.Ok 'echoed path line does not enter ACE parsing (exact match)'

Write-Host '== install lifecycle on fake OS =='
$runRoot = Join-Path $wd 'run'
New-RunRoot -Dir $runRoot
$cfg = New-Cfg -RunRoot $runRoot -SourceRoot $srcDir
$fake = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfg -SchtasksRunner $fake.Runner -IcaclsRunner (New-GoodIcaclsRunner)
$state = Get-Content -LiteralPath (Join-Path $runRoot 'broker\install-state.json') -Raw | ConvertFrom-Json
$createdCount = @(@($state.modules) | Where-Object { $_.classification -eq 'created' }).Count
Assert-True ($createdCount -ge 10) 'created manifest entries recorded (modules+keys+wrapper, R49-2 semantics)'
Assert-True ($state.modules[0].sha256 -is [string]) 'sha256 field is a string, not an array'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot 'run-broker.cmd')) 'wrapper generated'
$wrapperOnDisk = Get-Content -LiteralPath (Join-Path $runRoot 'run-broker.cmd') -Raw
Assert-True ($wrapperOnDisk -notmatch 'C:\\ProgramData\\UMShareRun') 'installed wrapper free of production paths'
Assert-True ($wrapperOnDisk -match '--share-port 8100') 'installed wrapper carries the share port'
$creates = @($fake.State['Calls'] | Where-Object { $_ -match ' /Create ' })
Assert-True ($creates.Count -ge 1) 'task created via controlled runner'
$keyContent = (Get-Content -LiteralPath (Join-Path $runRoot 'broker\broker-control-key') -Raw).Trim()
Assert-True ($keyContent -match '^[0-9a-f]{64}$') 'broker-control-key is a 64-hex secret'
Assert-Equal ([string]$state.task_wrapper) 'run-broker.cmd' 'manifest records the wrapper it owns'

Write-Host '== pre-existing identical file before install is shared =='
$runRoot2 = Join-Path $wd 'run2'
New-RunRoot -Dir $runRoot2
Copy-Item -LiteralPath (Join-Path $root 'share/broker_protocol.py') -Destination (Join-Path $runRoot2 'share/broker_protocol.py') -Force
$cfg2 = New-Cfg -RunRoot $runRoot2 -SourceRoot $srcDir
$fakeB = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot2 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfg2 -SchtasksRunner $fakeB.Runner -IcaclsRunner (New-GoodIcaclsRunner)
$stateB = Get-Content -LiteralPath (Join-Path $runRoot2 'broker\install-state.json') -Raw | ConvertFrom-Json
$protoEntry = @($stateB.modules | Where-Object { $_.file -eq 'share/broker_protocol.py' })[0]
Assert-Equal $protoEntry.classification 'shared' 'pre-existing identical protocol classified shared (kept on uninstall)'

Write-Host '== broker runtime records are NOT foreign content (contract §11.2) =='
$runRootM = Join-Path $wd 'runM'
New-RunRoot -Dir $runRootM
New-Item -ItemType Directory -Path (Join-Path $runRootM 'broker') -Force | Out-Null
# 受保护运行记录：broker 自己写的 lifecycle.json / maintenance.json（各含
# 原子 .tmp 形态）。重装遇到这些记录不得当作 foreign content 拒绝。
Set-Content -LiteralPath (Join-Path $runRootM 'broker\lifecycle.json') -Value '{"state":"stopped"}'
Set-Content -LiteralPath (Join-Path $runRootM 'broker\lifecycle.json.tmp') -Value ''
Set-Content -LiteralPath (Join-Path $runRootM 'broker\maintenance.json') -Value '{"state":"idle"}'
Set-Content -LiteralPath (Join-Path $runRootM 'broker\maintenance.json.tmp') -Value ''
$cfgM = New-Cfg -RunRoot $runRootM -SourceRoot $srcDir
$fakeM = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootM 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfgM -SchtasksRunner $fakeM.Runner -IcaclsRunner (New-GoodIcaclsRunner)
Assert-True (Test-Path -LiteralPath (Join-Path $runRootM 'broker\lifecycle.json')) 'lifecycle.json kept (owned record, not foreign)'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootM 'broker\maintenance.json')) 'maintenance.json kept (owned record, not foreign)'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootM 'share/broker_protocol.py')) 'install completed over the protected records'

Write-Host '== a truly foreign broker-dir file still refuses =='
$runRootN = Join-Path $wd 'runN'
New-RunRoot -Dir $runRootN
New-Item -ItemType Directory -Path (Join-Path $runRootN 'broker') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $runRootN 'broker\someone-elses.txt') -Value 'not ours'
$cfgN = New-Cfg -RunRoot $runRootN -SourceRoot $srcDir
$fakeN2 = New-FakeSchtasks -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgN -SchtasksRunner $fakeN2.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'foreign broker-dir content still refuses' 'foreign content'

Write-Host '== repeat install preserves ownership (no shared demotion) =='
$keyBefore2 = $keyContent
$fakeC = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd')
Install-Broker -Cfg $cfg -SchtasksRunner $fakeC.Runner -IcaclsRunner (New-GoodIcaclsRunner)
$stateC = Get-Content -LiteralPath (Join-Path $runRoot 'broker\install-state.json') -Raw | ConvertFrom-Json
$dist1 = @($state.modules | Group-Object classification | ForEach-Object { $_.Name + '=' + $_.Count }) -join ','
$dist2 = @($stateC.modules | Group-Object classification | ForEach-Object { $_.Name + '=' + $_.Count }) -join ','
Assert-Equal $dist2 $dist1 'repeat install preserves per-entry classification (R49-2)'
Assert-Equal ((Get-Content -LiteralPath (Join-Path $runRoot 'broker\broker-control-key') -Raw).Trim()) $keyBefore2 'existing key kept'
# R62-D: the task definition document is a manifest artifact; a repeat
# install encounters the IDENTICAL pre-existing file and keeps it (never a
# rebuild, never a shared demotion).
$defEntry1 = @($state.modules | Where-Object { $_.file -eq 'broker\task-definition.xml' })[0]
$defEntryC = @($stateC.modules | Where-Object { $_.file -eq 'broker\task-definition.xml' })[0]
Assert-True ($null -ne $defEntry1 -and $null -ne $defEntryC) 'the task definition document is recorded in the install manifest'
Assert-Equal $defEntryC.classification $defEntry1.classification 'the pre-existing identical task definition keeps its classification (R62-D, no rebuild)'

Write-Host '== absent-then-conflict copies NOTHING (R44-5) =='
$runRootX = Join-Path $wd 'runx'
New-RunRoot -Dir $runRootX
Set-Content -LiteralPath (Join-Path $runRootX 'share/runtime.py') -Value '# tampered'
$cfgX = New-Cfg -RunRoot $runRootX -SourceRoot $srcDir
$fakeX = New-FakeSchtasks
Assert-Throws { Install-Broker -Cfg $cfgX -SchtasksRunner $fakeX.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'conflict install throws' 'conflict'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRootX 'share/broker_protocol.py'))) 'earlier absent modules were NOT copied before the conflict'
Assert-Equal ($fakeX.State['Calls'] | Where-Object { $_ -match ' /Create ' }).Count 0 'conflict install registers no task'

Write-Host '== unknown same-name task refuses, zero modification (R44-5) =='
$runRootT = Join-Path $wd 'runt'
New-RunRoot -Dir $runRootT
$cfgT = New-Cfg -RunRoot $runRootT -SourceRoot $srcDir
$fakeT = New-FakeSchtasks -OwnedWrapper ''
Assert-Throws { Install-Broker -Cfg $cfgT -SchtasksRunner $fakeT.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'unknown same-name task refuses install' 'not owned by this install'
Assert-Equal ($fakeT.State['Calls'] | Where-Object { $_ -match ' /Create ' }).Count 0 'unknown task is never force-overwritten'

Write-Host '== task query failure refuses install (R44-5/R44-6) =='
$runRootQ = Join-Path $wd 'runq'
New-RunRoot -Dir $runRootQ
$cfgQ = New-Cfg -RunRoot $runRootQ -SourceRoot $srcDir
$fakeQ = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootQ 'run-broker.cmd') -QueryFailed $true
Assert-Throws { Install-Broker -Cfg $cfgQ -SchtasksRunner $fakeQ.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'task query failure refuses install' 'could not be established'

Write-Host '== failed task registration leaves recoverable owned state =='
$runRoot3 = Join-Path $wd 'run3'
New-RunRoot -Dir $runRoot3
# PRE-EXISTING EMPTY broker dir: the pre-change ACL must be recorded for a
# controlled recovery (R49-2 covers the pre-existing branch here).
New-Item -ItemType Directory -Path (Join-Path $runRoot3 'broker') -Force | Out-Null
$cfg3 = New-Cfg -RunRoot $runRoot3 -SourceRoot $srcDir
$fakeE = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot3 'run-broker.cmd') -CreateOk $false -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfg3 -SchtasksRunner $fakeE.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'task-create failure throws' 'schtasks create failed'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot3 'broker\install-state.json')) 'recovery manifest was persisted BEFORE the task write'
$stateE = Get-Content -LiteralPath (Join-Path $runRoot3 'broker\install-state.json') -Raw | ConvertFrom-Json
Assert-True ($null -ne $stateE.pre_dir_state) 'original dir state is PERSISTED in the manifest (R49-2)'
Assert-Equal ([string]$stateE.pre_dir_state.existed) 'True' 'pre-existing dir fact recorded'
Assert-True (@($stateE.pre_dir_state.aclOutput).Count -ge 1) 'original ACL rows are recorded for controlled recovery'
$keyEntry = @($stateE.modules | Where-Object { $_.file -eq 'broker\broker-control-key' })[0]
Assert-Equal $keyEntry.classification 'created' 'written key is recorded as created (not planned)'
$fakeF = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot3 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg3 -SchtasksRunner $fakeF.Runner -IcaclsRunner $null
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot3 'share/runtime.py'))) 'recovery uninstall removed owned copies'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot3 'broker'))) 'recovery uninstall removed empty broker dir'

Write-Host '== planned/empty-hash entries are NEVER delete permission (R49-2) =='
$runRoot5 = Join-Path $wd 'run5'
New-RunRoot -Dir $runRoot5
$cfg5 = New-Cfg -RunRoot $runRoot5 -SourceRoot $srcDir
$fake5a = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot5 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfg5 -SchtasksRunner $fake5a.Runner -IcaclsRunner (New-GoodIcaclsRunner)
# 模拟崩溃窗口：把 key 条目退回 planned+空哈希（失败窗口的真实清单形态）。
$state5 = Get-Content -LiteralPath (Join-Path $runRoot5 'broker\install-state.json') -Raw | ConvertFrom-Json
foreach ($m in @($state5.modules)) {
    if ($m.file -eq 'broker\broker-control-key') { $m.sha256 = ''; $m.classification = 'planned' }
}
Set-Content -LiteralPath (Join-Path $runRoot5 'broker\install-state.json') -Value ($state5 | ConvertTo-Json -Depth 4)
# 第三方随后写入了同名文件（崩溃窗口被替换的内容）。
Set-Content -LiteralPath (Join-Path $runRoot5 'broker\broker-control-key') -Value ('f' * 64)
$fake5b = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot5 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg5 -SchtasksRunner $fake5b.Runner -IcaclsRunner $null
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot5 'broker\broker-control-key')) 'empty-hash planned entry is NOT delete permission (replaced content kept)'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot5 'broker\install-state.json')) 'state file kept while unconfirmed entries exist'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot5 'share/runtime.py'))) 'created copies with hashes still removed'

Write-Host '== planned with EXPECTED hash is still not delete permission (R51.2) =='
$runRoot7 = Join-Path $wd 'run7'
New-RunRoot -Dir $runRoot7
$cfg7 = New-Cfg -RunRoot $runRoot7 -SourceRoot $srcDir
$fake7a = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot7 'run-broker.cmd') -CreateOk $false -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfg7 -SchtasksRunner $fake7a.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'planned-window install aborts (module write injected)' 'schtasks create failed'
# 把一个已 copy 的模块条目退回 planned+源哈希（写前崩溃窗口的真实清单形态），
# 文件已在盘上（copy 成功）——planned 语义：未经创建确认，绝不删除。
$state7 = Get-Content -LiteralPath (Join-Path $runRoot7 'broker\install-state.json') -Raw | ConvertFrom-Json
foreach ($m in @($state7.modules)) {
    if ($m.file -eq 'share/broker_host.py') { $m.classification = 'planned' }
}
Set-Content -LiteralPath (Join-Path $runRoot7 'broker\install-state.json') -Value ($state7 | ConvertTo-Json -Depth 4)
$fake7b = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot7 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg7 -SchtasksRunner $fake7b.Runner -IcaclsRunner $null
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot7 'share/broker_host.py')) 'planned with expected hash is NOT delete permission (same-content later file kept)'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot7 'broker\install-state.json')) 'state kept while planned entries exist'

Write-Host '== ACL recovery state machine across restarts (R53.2) =='
$originalAcl = @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                 'BUILTIN\Administrators:(OI)(CI)(F)',
                 'MACHINE\dacint:(OI)(CI)(RX)')
$changedAcl = @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                'BUILTIN\Administrators:(OI)(CI)(F)',
                'MACHINE\dacint:(OI)(CI)(RX)',
                'DACINT\um_share:(OI)(RX)')

# A1 crash leftover: a previous install saved the ORIGINAL ACL into
# broker-pre-acl.dat, changed the ACL, then the PROCESS DIED. A new
# install must roll the interrupted change back (restore) BEFORE its own
# change - never overwrite the original recovery material.
$runRootB1 = Join-Path $wd 'runB1'
New-RunRoot -Dir $runRootB1
New-Item -ItemType Directory -Path (Join-Path $runRootB1 'broker') -Force | Out-Null
$cleanChange = @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                  'BUILTIN\Administrators:(OI)(CI)(F)',
                  'MACHINE\dacint:(OI)(CI)(RX)')
$simRunner1 = New-SimulatedAclRunner -InitialAcl $changedAcl -ChangeAcl $cleanChange
# 模拟上次崩溃：备份文件在场（内容=原始 ACL），目录内当前 ACL 已是改动态。
[IO.File]::WriteAllLines((Join-Path $runRootB1 'broker-pre-acl.dat'),
    @('SIMULATED-ACL:') + @($originalAcl))
$crashState = @{ task_name = 'umshare-broker-test'; share_task = 'st';
    run_root = $runRootB1; broker_pipe_name = 'p'; instance_pipe_prefix = 'i';
    operator_account = 'dacint'; python_home = 'C:\py'; share_port = 9100;
    task_wrapper = 'run-broker.cmd';
    acl_recovery = @{ state = 'pending'; backup = 'broker-pre-acl.dat' };
    modules = @(); installed_at = 'crashed' }
Set-Content -LiteralPath (Join-Path $runRootB1 'broker\install-state.json') `
    -Value ($crashState | ConvertTo-Json -Depth 4)
$cfgB1 = New-Cfg -RunRoot $runRootB1 -SourceRoot $srcDir
$fakeB1 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB1 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfgB1 -SchtasksRunner $fakeB1.Runner -IcaclsRunner $simRunner1.Runner
$restoreCalls = @($simRunner1.State['Calls'] | Where-Object { $_ -match '/restore' })
$changeCalls = @($simRunner1.State['Calls'] | Where-Object { $_ -match '/inheritance:r' })
Assert-True ($restoreCalls.Count -ge 1) 'crash-left backup is RESTORED before any new ACL change'
$restoreIdx = $simRunner1.State['Calls'].IndexOf($restoreCalls[0])
$changeIdx = $simRunner1.State['Calls'].IndexOf($changeCalls[0])
Assert-True ($restoreIdx -lt $changeIdx) 'restore precedes the new change (call order)'
Assert-Equal (@($simRunner1.State['SimAcl']) -join '|') ($originalAcl -join '|') 'interrupted change ROLLED BACK to the original ACL'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRootB1 'broker-pre-acl.dat'))) 'recovery material consumed by the reentry restore'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootB1 'broker\install-state.json')) 'new install continues after the reentry recovery'

# A2 a SECOND install whose own /save FAILS: the ACL was never changed by
# THIS attempt, so the crash-left recovery material must NOT be applied -
# zero restores, the original backup file must survive byte-for-byte.
$runRootB2 = Join-Path $wd 'runB2'
New-RunRoot -Dir $runRootB2
New-Item -ItemType Directory -Path (Join-Path $runRootB2 'broker') -Force | Out-Null
$backupPath2 = Join-Path $runRootB2 'broker-pre-acl.dat'
[IO.File]::WriteAllLines($backupPath2, @('SIMULATED-ACL:') + @($originalAcl))
$simA = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB2 = New-Cfg -RunRoot $runRootB2 -SourceRoot $srcDir
$fakeB2 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB2 'run-broker.cmd') -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgB2 -SchtasksRunner $fakeB2.Runner -IcaclsRunner $simA.Runner } 'orphan backup refuses install' 'orphan ACL backup without an ownership record'
Assert-True (@($simA.State['Calls'] | Where-Object { $_ -match '/restore' }).Count -eq 0) 'orphan backup: ZERO restores (never auto-applied)'
Assert-Equal ((Get-Content -LiteralPath $backupPath2) -join '|') ((@('SIMULATED-ACL:') + $originalAcl) -join '|') 'orphan backup NOT touched by the refused install'

# A3 restore FAILURE leaves the pending state and BLOCKS cleanup: the
# backup file stays, install-state.json stays, and the install throws.
$runRootB3 = Join-Path $wd 'runB3'
New-RunRoot -Dir $runRootB3
New-Item -ItemType Directory -Path (Join-Path $runRootB3 'broker') -Force | Out-Null
$cleanChange = @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                  'BUILTIN\Administrators:(OI)(CI)(F)',
                  'MACHINE\dacint:(OI)(CI)(RX)')
$simRunner3 = New-SimulatedAclRunner -InitialAcl $originalAcl -ChangeAcl $cleanChange
$simRunner3.State['RestoreExitCode'] = 2
$cfgB3 = New-Cfg -RunRoot $runRootB3 -SourceRoot $srcDir
$fakeB3 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB3 'run-broker.cmd') -CreateOk $false -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgB3 -SchtasksRunner $fakeB3.Runner -IcaclsRunner $simRunner3.Runner } 'restore failure aborts install' 'schtasks create failed'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootB3 'broker-pre-acl.dat')) 'restore failure keeps the recovery evidence'
$stateB3 = Get-Content -LiteralPath (Join-Path $runRootB3 'broker\install-state.json') -Raw | ConvertFrom-Json
Assert-Equal ([string]$stateB3.acl_recovery.state) 'failed' 'recovery state recorded as failed'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootB3 'broker-pre-acl.dat')) 'backup kept before uninstall'
$fakeB3u = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB3 'run-broker.cmd')
Assert-Throws { Uninstall-Broker -Cfg $cfgB3 -SchtasksRunner $fakeB3u.Runner -IcaclsRunner $simRunner3.Runner } 'restore-failed state blocks the manifest deletion' 'operator review required'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootB3 'broker\install-state.json')) 'restore-failed state blocks the manifest deletion'

# A4 Phase 3 register failure AFTER a successful ACL change: the failure
# catch rolls the change back AND the recovery state advances to none
# (consumed) in the manifest.
$runRootB4 = Join-Path $wd 'runB4'
New-RunRoot -Dir $runRootB4
New-Item -ItemType Directory -Path (Join-Path $runRootB4 'broker') -Force | Out-Null
$cleanChange4 = @('NT AUTHORITY\SYSTEM:(OI)(CI)(F)',
                  'BUILTIN\Administrators:(OI)(CI)(F)',
                  'MACHINE\dacint:(OI)(CI)(RX)')
$simRunner4 = New-SimulatedAclRunner -InitialAcl $originalAcl -ChangeAcl $cleanChange4

$cfgB4 = New-Cfg -RunRoot $runRootB4 -SourceRoot $srcDir
$fakeB4 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB4 'run-broker.cmd') -CreateOk $false -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgB4 -SchtasksRunner $fakeB4.Runner -IcaclsRunner $simRunner4.Runner } 'register failure aborts' 'schtasks create failed'
Assert-Equal (@($simRunner4.State['SimAcl']) -join '|') ($originalAcl -join '|') 'Phase 3 failure rolled the ACL back'
$stateB4 = Get-Content -LiteralPath (Join-Path $runRootB4 'broker\install-state.json') -Raw | ConvertFrom-Json
Assert-Equal ([string]$stateB4.acl_recovery.state) 'none' 'recovery state advances to none after the successful rollback'

# A5 GC leftover: the terminal state was recorded (none) but the process
# died BEFORE deleting the backup. 'none' certifies the recovery was
# consumed/closed - the file is GC, never unapplied material. Reentry
# removes it and proceeds; no restore is ever attempted.
$runRootB5 = Join-Path $wd 'runB5'
New-RunRoot -Dir $runRootB5
New-Item -ItemType Directory -Path (Join-Path $runRootB5 'broker') -Force | Out-Null
[IO.File]::WriteAllLines((Join-Path $runRootB5 'broker-pre-acl.dat'),
    @('SIMULATED-ACL:') + @($originalAcl))
$gcState = @{ task_name = 'umshare-broker-test'; share_task = 'st';
    run_root = $runRootB5; broker_pipe_name = 'p'; instance_pipe_prefix = 'i';
    operator_account = 'dacint'; python_home = 'C:\py'; share_port = 9100;
    task_wrapper = 'run-broker.cmd';
    acl_recovery = @{ state = 'none'; backup = 'broker-pre-acl.dat' };
    modules = @(); installed_at = 'crashed-after-none' }
Set-Content -LiteralPath (Join-Path $runRootB5 'broker\install-state.json') `
    -Value ($gcState | ConvertTo-Json -Depth 4)
$sim5 = New-SimulatedAclRunner -InitialAcl $originalAcl -ChangeAcl $cleanChange
$cfgB5 = New-Cfg -RunRoot $runRootB5 -SourceRoot $srcDir
$fakeB5 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB5 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfgB5 -SchtasksRunner $fakeB5.Runner -IcaclsRunner $sim5.Runner
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRootB5 'broker-pre-acl.dat'))) 'state=none stale backup is GC on reentry (removed, never restored)'
Assert-True (@($sim5.State['Calls'] | Where-Object { $_ -match '/restore' }).Count -eq 0) 'GC leftover: ZERO restores'
Assert-True (@($sim5.State['Calls'] | Where-Object { $_ -match '/inheritance:r' }).Count -ge 1) 'GC leftover: fresh install proceeded with its own ACL change'

# A6 legacy manifest WITHOUT an acl_recovery record + a backup file: the
# backup provenance is unknown - refuse, never auto-apply.
$runRootB6 = Join-Path $wd 'runB6'
New-RunRoot -Dir $runRootB6
New-Item -ItemType Directory -Path (Join-Path $runRootB6 'broker') -Force | Out-Null
[IO.File]::WriteAllLines((Join-Path $runRootB6 'broker-pre-acl.dat'),
    @('SIMULATED-ACL:') + @($originalAcl))
$legacyState = @{ task_name = 'umshare-broker-test'; share_task = 'st';
    run_root = $runRootB6; broker_pipe_name = 'p'; instance_pipe_prefix = 'i';
    operator_account = 'dacint'; python_home = 'C:\py'; share_port = 9100;
    task_wrapper = 'run-broker.cmd';
    modules = @(); installed_at = 'legacy' }
Set-Content -LiteralPath (Join-Path $runRootB6 'broker\install-state.json') `
    -Value ($legacyState | ConvertTo-Json -Depth 4)
$sim6 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB6 = New-Cfg -RunRoot $runRootB6 -SourceRoot $srcDir
$fakeB6 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB6 'run-broker.cmd') -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgB6 -SchtasksRunner $fakeB6.Runner -IcaclsRunner $sim6.Runner } 'legacy manifest backup refuses install' 'orphan ACL backup without an ownership record'
Assert-True (@($sim6.State['Calls'] | Where-Object { $_ -match '/restore' }).Count -eq 0) 'legacy backup: ZERO restores (never auto-applied)'

# A7 corrupted recovery state refuses EVEN WITHOUT a backup file: the
# record itself is uninterpretable, so the ACL-change history is unknown.
$runRootB7 = Join-Path $wd 'runB7'
New-RunRoot -Dir $runRootB7
New-Item -ItemType Directory -Path (Join-Path $runRootB7 'broker') -Force | Out-Null
$badState = @{ task_name = 'umshare-broker-test'; share_task = 'st';
    run_root = $runRootB7; broker_pipe_name = 'p'; instance_pipe_prefix = 'i';
    operator_account = 'dacint'; python_home = 'C:\py'; share_port = 9100;
    task_wrapper = 'run-broker.cmd';
    acl_recovery = @{ state = 'garbage'; backup = '' };
    modules = @(); installed_at = 'corrupted' }
Set-Content -LiteralPath (Join-Path $runRootB7 'broker\install-state.json') `
    -Value ($badState | ConvertTo-Json -Depth 4)
$sim7 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB7 = New-Cfg -RunRoot $runRootB7 -SourceRoot $srcDir
$fakeB7 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB7 'run-broker.cmd') -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgB7 -SchtasksRunner $fakeB7.Runner -IcaclsRunner $sim7.Runner } 'unknown recovery state refuses even without a backup' 'unknown ACL recovery state'
Assert-True (@($sim7.State['Calls'] | Where-Object { $_ -match '/inheritance:r' }).Count -eq 0) 'corrupted state: ZERO ACL changes'

# A8 uninstall closes a state=none GC leftover too: the stale backup at the
# run root is removed during Gate 1.5, before the manifest is closed.
[IO.File]::WriteAllLines((Join-Path $runRootB4 'broker-pre-acl.dat'),
    @('SIMULATED-ACL:') + @($originalAcl))
$fakeB4u = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB4 'run-broker.cmd')
Uninstall-Broker -Cfg $cfgB4 -SchtasksRunner $fakeB4u.Runner -IcaclsRunner $null
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRootB4 'broker-pre-acl.dat'))) 'uninstall removes a state=none GC backup'

# A9 §57: a manifest WITH an acl_recovery property but a missing/empty/null
# state is a CORRUPTED record, not legacy - install refuses, the original
# manifest bytes survive untouched, and NOTHING is written (no ACL change,
# no task create, no temp/wrapper files).
function New-CorruptedRecoveryFixture {
    param([string]$Dir, [object]$Rec)
    New-RunRoot -Dir $Dir
    New-Item -ItemType Directory -Path (Join-Path $Dir 'broker') -Force | Out-Null
    $fixture = @{ task_name = 'umshare-broker-test'; share_task = 'st';
        run_root = $Dir; broker_pipe_name = 'p'; instance_pipe_prefix = 'i';
        operator_account = 'dacint'; python_home = 'C:\py'; share_port = 9100;
        task_wrapper = 'run-broker.cmd';
        acl_recovery = $Rec;
        modules = @(); installed_at = 'corrupted-empty-state' }
    Set-Content -LiteralPath (Join-Path $Dir 'broker\install-state.json') `
        -Value ($fixture | ConvertTo-Json -Depth 4)
}
function Assert-CorruptedRecoveryRefusal {
    param([string]$Dir, [hashtable]$Cfg, [object]$Fake, [object]$Sim,
          [string]$Name, [string]$ExpectInMessage)
    $manifestPath = Join-Path $Dir 'broker\install-state.json'
    $bytesBefore = [IO.File]::ReadAllBytes($manifestPath)
    Assert-Throws { Install-Broker -Cfg $Cfg -SchtasksRunner $Fake.Runner -IcaclsRunner $Sim.Runner } $Name $ExpectInMessage
    Assert-Equal ([IO.File]::ReadAllBytes($manifestPath).Length) $bytesBefore.Length "$Name`: corrupted manifest bytes PRESERVED"
    Assert-True (@($Sim.State['Calls'] | Where-Object { $_ -match '/inheritance:r' }).Count -eq 0) "$Name`: ZERO ACL changes"
    Assert-True (@($Fake.State['Calls'] | Where-Object { $_ -match '/Create' }).Count -eq 0) "$Name`: ZERO task creates"
    Assert-True (-not (Test-Path -LiteralPath ($manifestPath + '.tmp'))) "$Name`: no manifest temp write"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $Dir 'run-broker.cmd'))) "$Name`: no wrapper written"
}
$runRootB8 = Join-Path $wd 'runB8'
New-CorruptedRecoveryFixture -Dir $runRootB8 -Rec (@{})
$sim8 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB8 = New-Cfg -RunRoot $runRootB8 -SourceRoot $srcDir
$fakeB8 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB8 'run-broker.cmd') -TaskAbsent $true
Assert-CorruptedRecoveryRefusal -Dir $runRootB8 -Cfg $cfgB8 -Fake $fakeB8 -Sim $sim8 'acl_recovery object WITHOUT state refuses' 'unknown ACL recovery state'
# empty-string state: equally corrupted (not a legacy signal)
$runRootB9 = Join-Path $wd 'runB9'
New-CorruptedRecoveryFixture -Dir $runRootB9 -Rec (@{ state = ''; backup = '' })
$sim9 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB9 = New-Cfg -RunRoot $runRootB9 -SourceRoot $srcDir
$fakeB9 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB9 'run-broker.cmd') -TaskAbsent $true
Assert-CorruptedRecoveryRefusal -Dir $runRootB9 -Cfg $cfgB9 -Fake $fakeB9 -Sim $sim9 'acl_recovery EMPTY state refuses' 'unknown ACL recovery state'
# null state (injected at JSON level: ConvertTo-Json cannot emit "state": null)
$runRootB10 = Join-Path $wd 'runB10'
New-CorruptedRecoveryFixture -Dir $runRootB10 -Rec (@{ backup = 'broker-pre-acl.dat' })
$manifestB10 = Join-Path $runRootB10 'broker\install-state.json'
$nullStateJson = (Get-Content -LiteralPath $manifestB10 -Raw).Replace('"backup"', '"state": null, "backup"')
Set-Content -LiteralPath $manifestB10 -Value $nullStateJson
$sim10 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB10 = New-Cfg -RunRoot $runRootB10 -SourceRoot $srcDir
$fakeB10 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB10 'run-broker.cmd') -TaskAbsent $true
Assert-CorruptedRecoveryRefusal -Dir $runRootB10 -Cfg $cfgB10 -Fake $fakeB10 -Sim $sim10 'acl_recovery NULL state refuses' 'unknown ACL recovery state'
Assert-True ((Get-Content -LiteralPath $manifestB10 -Raw) -match '"state":\s*null') 'null-state fixture really contains JSON null'
# pending with an EMPTY backup path is corrupted at the RECORD level (the
# backup-file-missing refusal never gets a chance to fire)
$runRootB11 = Join-Path $wd 'runB11'
New-CorruptedRecoveryFixture -Dir $runRootB11 -Rec (@{ state = 'pending'; backup = '' })
$sim11 = New-SimulatedAclRunner -InitialAcl $originalAcl
$cfgB11 = New-Cfg -RunRoot $runRootB11 -SourceRoot $srcDir
$fakeB11 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB11 'run-broker.cmd') -TaskAbsent $true
Assert-CorruptedRecoveryRefusal -Dir $runRootB11 -Cfg $cfgB11 -Fake $fakeB11 -Sim $sim11 'pending with empty backup rel refuses' 'requires a backup path'
# A10 §57: a LEGAL state=none record with NO backup file keeps the
# compatibility contract - install proceeds (the recovery is closed).
$runRootB12 = Join-Path $wd 'runB12'
New-CorruptedRecoveryFixture -Dir $runRootB12 -Rec (@{ state = 'none'; backup = '' })
$sim12 = New-SimulatedAclRunner -InitialAcl $originalAcl -ChangeAcl $cleanChange
$cfgB12 = New-Cfg -RunRoot $runRootB12 -SourceRoot $srcDir
$fakeB12 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootB12 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfgB12 -SchtasksRunner $fakeB12.Runner -IcaclsRunner $sim12.Runner
Assert-True (@($sim12.State['Calls'] | Where-Object { $_ -match '/inheritance:r' }).Count -ge 1) 'legal none record without backup: install proceeded'
Assert-True (@($sim12.State['Calls'] | Where-Object { $_ -match '/restore' }).Count -eq 0) 'legal none record without backup: ZERO restores'



Write-Host '== shared keys/wrapper are NEVER claimed as created (R51.2) =='
$runRoot8 = Join-Path $wd 'run8'
New-RunRoot -Dir $runRoot8
# 既有两把 Key + 相同 wrapper（无前轮清单 = shared 语义）。
New-Item -ItemType Directory -Path (Join-Path $runRoot8 'broker') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $runRoot8 'broker\broker-control-key') -Value ('a' * 64)
Set-Content -LiteralPath (Join-Path $runRoot8 'instance-channel-key') -Value ('b' * 64)
$cfg8 = New-Cfg -RunRoot $runRoot8 -SourceRoot $srcDir
$wrapper8 = New-BrokerWrapperContent -Cfg $cfg8
[IO.File]::WriteAllText((Join-Path $runRoot8 'run-broker.cmd'), $wrapper8, (New-Object System.Text.UTF8Encoding($false)))
$fake8a = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot8 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfg8 -SchtasksRunner $fake8a.Runner -IcaclsRunner (New-GoodIcaclsRunner)
$state8 = Get-Content -LiteralPath (Join-Path $runRoot8 'broker\install-state.json') -Raw | ConvertFrom-Json
$keyCls = (@($state8.modules | Where-Object { $_.file -eq 'broker\broker-control-key' })[0]).classification
$instKeyCls = (@($state8.modules | Where-Object { $_.file -eq 'instance-channel-key' })[0]).classification
$wrapCls = (@($state8.modules | Where-Object { $_.file -eq 'run-broker.cmd' })[0]).classification
Assert-Equal $keyCls 'shared' 'pre-existing control key stays shared (never claimed)'
Assert-Equal $instKeyCls 'shared' 'pre-existing instance key stays shared'
Assert-Equal $wrapCls 'shared' 'pre-existing identical wrapper stays shared'
$fake8b = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot8 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg8 -SchtasksRunner $fake8b.Runner -IcaclsRunner $null
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot8 'broker\broker-control-key')) 'shared control key KEPT on uninstall'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot8 'instance-channel-key')) 'shared instance key KEPT on uninstall'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot8 'run-broker.cmd')) 'shared wrapper KEPT on uninstall'

Write-Host '== tail planned bad path: ZERO task ops (R51.2 gate-0.5 scope) =='
$runRoot9 = Join-Path $wd 'run9'
New-RunRoot -Dir $runRoot9
$cfg9 = New-Cfg -RunRoot $runRoot9 -SourceRoot $srcDir
$fake9a = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot9 'run-broker.cmd') -TaskAbsent $true
Install-Broker -Cfg $cfg9 -SchtasksRunner $fake9a.Runner -IcaclsRunner (New-GoodIcaclsRunner)
$state9 = Get-Content -LiteralPath (Join-Path $runRoot9 'broker\install-state.json') -Raw | ConvertFrom-Json
$state9.modules += @{ file = 'C:\abs\escape2.txt'; sha256 = ''; classification = 'planned' }
Set-Content -LiteralPath (Join-Path $runRoot9 'broker\install-state.json') -Value ($state9 | ConvertTo-Json -Depth 4)
$fake9b = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot9 'run-broker.cmd')
Assert-Throws { Uninstall-Broker -Cfg $cfg9 -SchtasksRunner $fake9b.Runner -IcaclsRunner $null } 'tail planned bad path refuses' 'whitelist'
Assert-Equal $fake9b.State['Calls'].Count 0 'tail planned bad path: ZERO task operations'

Write-Host '== ACL failure window: original state recorded BEFORE any ACL change (R51.3) =='
$runRootA = Join-Path $wd 'runA'
New-RunRoot -Dir $runRootA
New-Item -ItemType Directory -Path (Join-Path $runRootA 'broker') -Force | Out-Null
$cfgA = New-Cfg -RunRoot $runRootA -SourceRoot $srcDir
# 序列 fake：第 1 次 icacls=原状态探测(成功) / 第 2 次=设置(成功) / 第 3 次=验证(坏输出)
$seq = New-SequenceIcaclsRunner
$fakeA = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootA 'run-broker.cmd') -TaskAbsent $true
Assert-Throws { Install-Broker -Cfg $cfgA -SchtasksRunner $fakeA.Runner -IcaclsRunner $seq.Runner } 'ACL verify failure aborts' 'ACL verification failed'
Assert-True (Test-Path -LiteralPath (Join-Path $runRootA 'broker\install-state.json')) 'manifest (with pre_dir_state) persisted BEFORE the ACL change'
$stateA = Get-Content -LiteralPath (Join-Path $runRootA 'broker\install-state.json') -Raw | ConvertFrom-Json
Assert-True (@($stateA.pre_dir_state.aclOutput).Count -ge 1) 'original ACL rows available for controlled recovery after failure'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRootA 'broker\pre-acl.dat'))) 'recovery file consumed by the successful rollback'
Assert-True (@($seq.State['Calls'] | Where-Object { $_ -match '/restore' }).Count -ge 1) 'failed ACL change was ROLLED BACK via icacls /restore'

Write-Host '== task race: foreign task appearing after inventory is never overwritten (R49-4) =='
$runRoot6 = Join-Path $wd 'run6'
New-RunRoot -Dir $runRoot6
$cfg6 = New-Cfg -RunRoot $runRoot6 -SourceRoot $srcDir
$fake6 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot6 'run-broker.cmd') -ForeignOnCreate $true
Assert-Throws { Install-Broker -Cfg $cfg6 -SchtasksRunner $fake6.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'race: foreign task at registration refuses' 'not owned by this install'
Assert-Equal ($fake6.State['Calls'] | Where-Object { $_ -match ' /Create ' }).Count 0 'race: NO /Create (no force overwrite)'

Write-Host '== task shape: args/principal/dual-exec refuse (R51.5) =='
$runRootT2 = Join-Path $wd 'runT2'
New-RunRoot -Dir $runRootT2
$cfgT2 = New-Cfg -RunRoot $runRootT2 -SourceRoot $srcDir
$fakeT2a = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootT2 'run-broker.cmd') -TaskArguments '--broker-dir OTHER'
Assert-Throws { Install-Broker -Cfg $cfgT2 -SchtasksRunner $fakeT2a.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'foreign task arguments refuse' 'not owned by this install'
$fakeT2b = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootT2 'run-broker.cmd') -TaskPrincipal 'S-1-5-32-544'
Assert-Throws { Install-Broker -Cfg $cfgT2 -SchtasksRunner $fakeT2b.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'foreign principal refuses' 'not owned by this install'
$fakeT2c = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootT2 'run-broker.cmd') -DualExec $true
Assert-Throws { Install-Broker -Cfg $cfgT2 -SchtasksRunner $fakeT2c.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'dual exec refuses' 'expected exactly 1'
$fakeT2d = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootT2 'run-broker.cmd') -ComHandler $true
Assert-Throws { Install-Broker -Cfg $cfgT2 -SchtasksRunner $fakeT2d.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'ComHandler action sibling refuses (R53.4)' 'expected exactly 1 Exec'
Assert-Equal $fakeT2d.State['Calls'].Count 1 'ComHandler refuse: only the ownership query ran, ZERO create'
$fakeT2e = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootT2 'run-broker.cmd') -PrincipalCount 2
Assert-Throws { Install-Broker -Cfg $cfgT2 -SchtasksRunner $fakeT2e.Runner -IcaclsRunner (New-GoodIcaclsRunner) } 'multiple principals refuse (R53.4)' 'expected exactly 1'
Assert-Equal $fakeT2e.State['Calls'].Count 1 'multi-principal refuse: only the ownership query ran, ZERO create'

Write-Host '== register binds wrapper CONTENT before /Create (R53.3) =='
$runRootR1 = Join-Path $wd 'runR1'
New-RunRoot -Dir $runRootR1
$cfgR1 = New-Cfg -RunRoot $runRootR1 -SourceRoot $srcDir
# The task DEFINITION reports ownership of this wrapper path, but the FILE
# on disk was replaced after the inventory - registration must refuse on
# content, never launch an unverified payload as SYSTEM.
Set-Content -LiteralPath (Join-Path $runRootR1 'run-broker.cmd') -Value '# tampered after inventory'
$fakeR1 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootR1 'run-broker.cmd')
Assert-Throws { Register-BrokerTask -Cfg $cfgR1 -WrapperPath (Join-Path $runRootR1 'run-broker.cmd') -SchtasksRunner $fakeR1.Runner -PrevOwned $true } 'register refuses replaced wrapper content' 'wrapper content differs'
Assert-Equal $fakeR1.State['Calls'].Count 1 'replaced wrapper: only the identity query ran, ZERO create'
# Same ownership evidence but the wrapper FILE is absent - refuse, never
# register a launch entry for a nonexistent payload.
$runRootR2 = Join-Path $wd 'runR2'
New-RunRoot -Dir $runRootR2
$cfgR2 = New-Cfg -RunRoot $runRootR2 -SourceRoot $srcDir
$fakeR2 = New-FakeSchtasks -OwnedWrapper (Join-Path $runRootR2 'run-broker.cmd')
Assert-Throws { Register-BrokerTask -Cfg $cfgR2 -WrapperPath (Join-Path $runRootR2 'run-broker.cmd') -SchtasksRunner $fakeR2.Runner -PrevOwned $true } 'register refuses missing wrapper file' 'wrapper file missing'
Assert-Equal $fakeR2.State['Calls'].Count 1 'missing wrapper: only the identity query ran, ZERO create'

Write-Host '== uninstall: no manifest -> ZERO task operations (R44-6) =='
$runRoot4 = Join-Path $wd 'run4'
New-RunRoot -Dir $runRoot4
Set-Content -LiteralPath (Join-Path $runRoot4 'instance-channel-key') -Value ('a' * 64)
$cfg4 = New-Cfg -RunRoot $runRoot4 -SourceRoot $srcDir
$fakeN = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot4 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg4 -SchtasksRunner $fakeN.Runner -IcaclsRunner $null
Assert-Equal $fakeN.State['Calls'].Count 0 'no manifest: the task is NEVER touched'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot4 'instance-channel-key')) 'absent state file: known artifacts NOT removed'

Write-Host '== uninstall gates (stop/delete evidence) =='
$fakeG = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd') -QueryState 'Running'
$before = @(Get-ChildItem -LiteralPath $runRoot -Recurse -File).Count
Assert-Throws { Uninstall-Broker -Cfg $cfg -SchtasksRunner $fakeG.Runner -IcaclsRunner $null } 'stop unconfirmed blocks deletion' 'not confirmed'
Assert-Equal (@(Get-ChildItem -LiteralPath $runRoot -Recurse -File).Count) $before 'no file removed while task Running'
$fakeH = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd') -DeleteOk $false
Assert-Throws { Uninstall-Broker -Cfg $cfg -SchtasksRunner $fakeH.Runner -IcaclsRunner $null } 'delete unconfirmed blocks deletion' 'not confirmed'

Write-Host '== uninstall: orphaned broker process refuses (D5) =='
$fakeO = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd') -BrokerPidsJson '[{"ProcessId": 77, "CommandLine": "C:/run/um-share-python.exe share/broker_server.py --broker-dir x", "ExecutablePath": "C:/run/um-share-python.exe", "CreationDate": "20260909000000.000000+480", "OwnerDomain": "MACHINE", "OwnerUser": "dacint"}]'
Assert-Throws { Uninstall-Broker -Cfg $cfg -SchtasksRunner $fakeO.Runner -IcaclsRunner $null } 'orphan broker process blocks deletion' 'broker process still alive'

Write-Host '== uninstall: bad manifest tail path refuses BEFORE task action (D2) =='
$stateTail = Get-Content -LiteralPath (Join-Path $runRoot2 'broker\install-state.json') -Raw | ConvertFrom-Json
$stateTail.modules += @{ file = 'C:\abs\escape.txt'; sha256 = ''; classification = 'owned' }
Set-Content -LiteralPath (Join-Path $runRoot2 'broker\install-state.json') -Value ($stateTail | ConvertTo-Json -Depth 4)
$fakeTail = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot2 'run-broker.cmd')
Assert-Throws { Uninstall-Broker -Cfg $cfg2 -SchtasksRunner $fakeTail.Runner -IcaclsRunner $null } 'bad tail path refuses before task ops' 'whitelist'
Assert-Equal $fakeTail.State['Calls'].Count 0 'bad tail path: ZERO task operations happened'
# 还原清单（后续用例不受污染）
$stateTail.modules = @($stateTail.modules | Where-Object { $_.file -ne 'C:\abs\escape.txt' })
Set-Content -LiteralPath (Join-Path $runRoot2 'broker\install-state.json') -Value ($stateTail | ConvertTo-Json -Depth 4)

Write-Host '== uninstall: task/manifest mismatch refuses ==' 
$cfgWrong = New-Cfg -RunRoot $runRoot -SourceRoot $srcDir
$cfgWrong['TaskName'] = 'umshare-broker-OTHER'
$fakeW = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd')
Assert-Throws { Uninstall-Broker -Cfg $cfgWrong -SchtasksRunner $fakeW.Runner -IcaclsRunner $null } 'manifest task mismatch refuses' 'does not match the manifest task'
Assert-Equal $fakeW.State['Calls'].Count 0 'mismatched task namespace is never operated'

Write-Host '== uninstall keeps shared entries, foreign files, post-install edits =='
$protoDst = Join-Path $runRoot 'share/broker_protocol.py'
$foreign = Join-Path $runRoot 'broker\someone-elses.txt'
Set-Content -LiteralPath $foreign -Value 'not ours'
$edited = Join-Path $runRoot 'share/management.py'
Set-Content -LiteralPath $edited -Value '# edited by a later task'
$fakeI = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg -SchtasksRunner $fakeI.Runner -IcaclsRunner $null
Assert-True (Test-Path -LiteralPath $foreign) 'unknown leftover in broker dir kept (dir NOT recursed)'
Assert-True (Test-Path -LiteralPath $edited) 'owned file edited after install is KEPT'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot 'share/runtime.py'))) 'unmodified owned copy removed'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot 'broker\broker-control-key'))) 'owned key removed'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot 'broker\install-state.json')) 'ownership record KEPT while owned entries were modified (traceability)'
Assert-True (Test-Path -LiteralPath (Join-Path $runRoot 'broker')) 'broker dir kept while owned entries were modified'

Write-Host '== uninstall keeps the PRE-EXISTING shared protocol and keys (runRoot2) =='
$protoDst2 = Join-Path $runRoot2 'share/broker_protocol.py'
$protoHashBefore2 = (Get-FileHash -LiteralPath $protoDst2 -Algorithm SHA256).Hash
$fakeJ = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot2 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg2 -SchtasksRunner $fakeJ.Runner -IcaclsRunner $null
Assert-True (Test-Path -LiteralPath $protoDst2) 'pre-existing identical protocol kept after uninstall'
Assert-Equal (Get-FileHash -LiteralPath $protoDst2 -Algorithm SHA256).Hash $protoHashBefore2 'kept protocol content unchanged'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot2 'share/runtime.py'))) 'owned copies on the same root removed'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $runRoot2 'broker\install-state.json'))) 'ownership record closed on clean uninstall'

Write-Host '== repeat uninstall is idempotent and safe =='
$fakeK = New-FakeSchtasks -OwnedWrapper (Join-Path $runRoot2 'run-broker.cmd')
Uninstall-Broker -Cfg $cfg2 -SchtasksRunner $fakeK.Runner -IcaclsRunner $null
Assert-Equal $fakeK.State['Calls'].Count 0 'repeat uninstall with no manifest performs ZERO operations'

Write-Host '== R218: the ENTRY real runner under a caller ErrorActionPreference=Stop =='
# Extract the REAL New-RealCommandRunner definition from install-share-broker.ps1
# (the entry cannot be dot-sourced: its top-level flow installs) and invoke it
# in THIS scope, which also runs with Stop - reproducing the production scope
# where a native stderr line under 2>&1 used to become a terminating
# NativeCommandError (schtasks /Query of an absent task -> install exit 1).
$entryPath = Join-Path $root 'um-share-deploy\install-share-broker.ps1'
$entryErrs = $null
$entryAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $entryPath, [ref]$null, [ref]$entryErrs)
if ($entryErrs -and $entryErrs.Count -gt 0) {
    Assert-True $false 'entry file parses cleanly'
} else {
    Assert-True $true 'entry file parses cleanly'
}
$fnAst = $entryAst.Find(
    { param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
             $a.Name -eq 'New-RealCommandRunner' }, $true)
Assert-True ($null -ne $fnAst) 'entry defines New-RealCommandRunner'
if ($fnAst) {
    Invoke-Expression $fnAst.Extent.Text
    $r218 = New-RealCommandRunner

    $r1 = & $r218 @('cmd', '/c', 'echo expected-absence-evidence>&2 & exit /b 0')
    Assert-Equal $r1.ExitCode 0 'stderr line + exit 0: exit code is the verdict (no NativeCommandError)'
    Assert-True ((@($r1.Output) -join "`n") -match 'expected-absence-evidence') 'stderr text stays a captured record'

    $r2 = & $r218 @('cmd', '/c', 'echo absence-marker-r218>&2 & exit /b 2')
    Assert-Equal $r2.ExitCode 2 'stderr + nonzero: exact exit code preserved (refusal, not absence)'
    Assert-True ((@($r2.Output) -join "`n") -match 'absence-marker-r218') 'nonzero stderr text captured as evidence'

    $r3 = & $r218 @('cmd', '/c', 'echo ok-stdout')
    Assert-Equal $r3.ExitCode 0 'pure success: exit 0'
    Assert-True ((@($r3.Output) -join "`n") -match 'ok-stdout') 'stdout captured'

    $r4 = & $r218 @('definitely-not-a-real-tool-r218-xyz', '--flag')
    Assert-True ($null -ne $r4) 'failed launch returns a structured record (no throw under caller Stop)'
    Assert-Equal $r4.ExitCode 128 'failed launch: explicit refusal code (never a fabricated 0)'
    # PS 5.1 emits the launcher CommandNotFound to the console error stream,
    # NOT into the 2>&1 capture - the refusal verdict is the exit code, the
    # downstream consumers refuse on unparsable/empty output as well.
}

if ($script:failures -gt 0) {
    Write-Host ("install-share-broker offline tests: {0} FAILURES" -f $script:failures) -ForegroundColor Red
    exit 1
}
Write-Host 'install-share-broker offline tests: ALL PASS' -ForegroundColor Green
exit 0
