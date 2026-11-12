# authoritative_readings.ps1 - ELEVATED, READ-ONLY authoritative readings for the
# 8096 restart window (input for `share_chain_snapshot.py --authoritative <json>`).
#
# WHY THIS EXISTS (maintainer review of execution round 9, 2026-09-28):
#   The non-elevated snapshot records `window_signals.watchdog_task` as
#   `undecidable` (a SYSTEM scheduled task is invisible to a non-elevated
#   identity), and it cannot read the share limits (WFP + firewall) or the 8100 run
#   log at all. The comparison gate requires a readable state on BOTH sides, so the
#   window must be entered with an authoritative reading taken BEFORE the restart
#   and merged with the SAME convention AFTER it. An `undecidable` side never
#   counts as a pass.
#
# READ-ONLY CONTRACT (what this script promises):
#   * NO file is written by default. The one and only write path is the explicit
#     `-Out <path>` switch (UTF-8 without BOM, so python's json can read it back);
#     without `-Out` the JSON goes to stdout only.
#   * NO scheduled task is created / started / stopped / unregistered.
#   * NO service is restarted, NO process is started or killed, NO key is
#     generated, NO model request is sent, `ai-gateway/data/` is never touched.
#   * The WFP / firewall judgement is NOT re-implemented here: this script
#     dot-sources `um-share-deploy/watchdog-umshare.ps1 -FunctionsOnly` and calls
#     its own read-only `Get-LimitsState` / `Get-FirewallState` (the watchdog's
#     limits check), so the probe and the watchdog cannot drift apart.
#   * `raw.txt` content is NEVER read (path + mtime + size only); no secret is
#     ever printed (the key file is not read here at all).
#   * `-RunningScriptPath <path>` (optional, ADDITIVE, default OFF): when given,
#     that path's metadata (existence / size / mtime / sha256) is read and compared
#     BYTE-FOR-BYTE with the repo script; the verdict lands in the top-level
#     `running_script_check` section. Without the parameter that section does not
#     exist at all and the output is unchanged. Still read-only: no write, no task
#     change, no restart, no process start/kill.
#
# USAGE (elevated identity; stdout is one JSON document):
#   powershell -NoProfile -ExecutionPolicy Bypass -File `
#     D:\git\github\personal_files\SelfUseAI\ai-gateway\tools\um-share\authoritative_readings.ps1
#   # hand-over form (explicit write switch; run BEFORE and AFTER the window):
#   ... -Out output\ai-gw-round10-20260928\authoritative-before.json
#   # running-copy cross-check (the protected run copy the watchdog task runs):
#   ... -RunningScriptPath C:\ProgramData\UMShareRun\watchdog-umshare.ps1
#
# MERGE (records source + capture time; the snapshot itself stays read-only):
#   python ai-gateway/tools/um-share/share_chain_snapshot.py `
#     --out output/ai-gw-round10-20260928/snapshot-before.json `
#     --authoritative output/ai-gw-round10-20260928/authoritative-before.json
#
# JSON shape: the top-level `window_signals` maps signal name -> entry with the
# SAME names and the SAME shape as `share_chain_snapshot.py`'s window_signals
# (`status` is always one of ok / absent / undecidable; a read failure is NEVER
# folded into a definite fact).
param(
    [string]$RunRoot = 'C:\ProgramData\UMShareRun',
    [string]$DeployDir = '',
    [string]$WatchdogScript = '',
    # 可选：受保护的**运行副本**（deploy-elevated.ps1 的 Watchdog Stage 把仓库脚本拷到
    # RunRoot 后以 SYSTEM 运行的那一份）。给定时只做只读比较，见下面 (0) 节。
    [string]$RunningScriptPath = '',
    [string]$WatchdogTaskName = 'umshare-watchdog',
    [string]$FirewallRule = 'um-share-outbound-block',
    [string]$AppExe = '',
    [string]$WfpHelper = '',
    [string]$BackendDir = '',
    [string]$RawTxtPath = '',
    [string]$Out = ''
)
$ErrorActionPreference = 'Continue'

# .NET ticks between 0001-01-01 and 1970-01-01 (same constant the python side
# uses for the CIM start time: share_chain_snapshot._DOTNET_EPOCH_SECONDS).
$script:DotnetEpochSeconds = 62135596800.0

function Get-UtcEpoch([datetime]$utc) {
    # Ticks-based conversion: `[DateTimeOffset]::new([datetime])` binds to the
    # wrong constructor overload under Windows PowerShell 5.1.
    return ([double]$utc.ToUniversalTime().Ticks / 10000000.0) `
        - $script:DotnetEpochSeconds
}

function Get-IsoOffset([datetimeoffset]$when) {
    return $when.ToOffset([TimeSpan]::FromHours(8)).ToString(
        'yyyy-MM-ddTHH:mm:sszzz')
}

function Get-ErrorName($err) {
    try { return $err.Exception.GetType().Name } catch { return 'unknown' }
}

function Test-PathQuiet([string]$path) {
    # Test-Path writes a non-terminating error to stderr on access denial; a
    # read-only probe must not look like a failure.
    try {
        return [bool](Test-Path -LiteralPath $path -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Get-MtimeFacts($item) {
    $out = @{ mtime = $null; mtime_iso = $null
              mtime_reason = 'mtime_unavailable' }
    try {
        $out.mtime = Get-UtcEpoch $item.LastWriteTimeUtc
        $out.mtime_iso = $item.LastWriteTimeUtc.ToString('o')
        $out.mtime_reason = 'ok'
    } catch {
        $out.mtime_reason = 'mtime_unavailable:' + (Get-ErrorName $_)
    }
    return $out
}

function Get-Sha256Hex($bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString(
            $sha.ComputeHash($bytes)).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
}

function Get-FileByteFacts([string]$path) {
    # 只读读取一个文件的元数据与字节（存在 / 大小 / mtime / sha256）。
    # `reason` 是三态口径的唯一来源：'ok' 才是「读到了」；'missing' = 权威的
    # 不存在；'unreadable:<异常名>' = **读不到**（权限拒绝、共享冲突……）。
    # 读不到绝不折成「不存在」，也不折成「空文件」：exists 记 $null。
    $out = @{ exists = $null; size = $null; mtime = $null; mtime_iso = $null
              mtime_reason = 'not_read'; sha256 = $null; bytes = $null
              reason = 'ok'; error = '' }
    try {
        $item = Get-Item -LiteralPath $path -ErrorAction Stop
    } catch {
        $name = Get-ErrorName $_
        $out.error = $_.Exception.Message
        if ($name -eq 'ItemNotFoundException') {
            # 确定事实：这个路径上现在没有文件（是否曾经有过、由谁删的，不在此列）。
            $out.exists = $false
            $out.reason = 'missing'
        } else {
            $out.reason = 'unreadable:' + $name
        }
        return $out
    }
    $out.exists = $true
    $mtimeFacts = Get-MtimeFacts $item
    $out.mtime = $mtimeFacts.mtime
    $out.mtime_iso = $mtimeFacts.mtime_iso
    $out.mtime_reason = $mtimeFacts.mtime_reason
    try {
        $bytes = [System.IO.File]::ReadAllBytes($path)
    } catch {
        # 存在但读不到（目录、权限拒绝、共享冲突……）：sha256 与逐字节比较都不可判。
        $out.reason = 'unreadable:' + (Get-ErrorName $_)
        $out.error = $_.Exception.Message
        return $out
    }
    $out.size = $bytes.Length
    $out.sha256 = Get-Sha256Hex $bytes
    $out.bytes = $bytes
    return $out
}

function Test-ByteArrayEqual($left, $right) {
    # 逐字节比较（长度不同即不同）。
    if ($left.Length -ne $right.Length) { return $false }
    for ($i = 0; $i -lt $left.Length; $i++) {
        if ($left[$i] -ne $right[$i]) { return $false }
    }
    return $true
}

if ($DeployDir -eq '') {
    $DeployDir = [System.IO.Path]::GetFullPath(
        (Join-Path $PSScriptRoot '..\..\um-share-deploy'))
}
if ($WatchdogScript -eq '') {
    $WatchdogScript = Join-Path $DeployDir 'watchdog-umshare.ps1'
}
if ($AppExe -eq '') { $AppExe = Join-Path $RunRoot 'um-share-python.exe' }
if ($WfpHelper -eq '') { $WfpHelper = Join-Path $RunRoot 'wfp_helper.ps1' }
if ($RawTxtPath -eq '') {
    $RawTxtPath = Join-Path $RunRoot 'run\live\raw.txt'
}

# ------------------------------------------- (0) 运行副本核对（可选，只读，默认关闭）
# WHY (maintainer requirement 2026-09-28, before the 8096 window): this probe reads
# the REPO watchdog script by default, but the task `umshare-watchdog` runs the
# PROTECTED RUN COPY that deploy-elevated.ps1's Watchdog Stage mirrored there
# (deploy-elevated.ps1:894-930 -> RunRoot\watchdog-umshare.ps1, hash-verified at
# copy time, launched as SYSTEM with -File). A clean repo script therefore does NOT
# prove that the copy the SYSTEM task actually runs is clean. `-RunningScriptPath`
# lets one elevated reading answer that question without any second tool.
#
# Three-state vocabulary of THIS section (deliberately NOT the window_signals
# ok/absent/undecidable vocabulary — a verified difference is a definite fact that
# must never be named `absent`):
#   ok          = both files read and byte-for-byte identical (matches_repo = true)
#   mismatch    = both files read and byte-for-byte different (matches_repo = false)
#   undecidable = cannot read / does not exist / same path on both sides
#                 (matches_repo = null) — never folded into "matching", and never
#                 folded into "different".
# A missing run copy is recorded truthfully as exists=false, but the comparison
# question itself is undecidable (there is nothing to compare), so matches_repo stays
# null. `exists` is a fact; `matches_repo` is the verdict.
# This section proves DISK BYTES only: it does NOT prove what the running watchdog
# process loaded, nor the run copy's ACL/owner.
$runCheckEnabled = ($RunningScriptPath -ne '')
$runningCheck = $null
if ($runCheckEnabled) {
    $repoFacts = Get-FileByteFacts $WatchdogScript
    $runFacts = Get-FileByteFacts $RunningScriptPath
    $repoFull = ''
    $runFull = ''
    try { $repoFull = [System.IO.Path]::GetFullPath($WatchdogScript) } catch { }
    try { $runFull = [System.IO.Path]::GetFullPath($RunningScriptPath) } catch { }
    $runStatus = 'undecidable'
    $runReason = ''
    $runDetail = ''
    $runMatches = $null
    if ($repoFull -ne '' -and $repoFull -eq $runFull) {
        # 同一路径自比恒为真，证明不了运行副本与仓库一致：按 undecidable 记。
        $runReason = 'same_path'
        $runDetail = ("运行副本路径与仓库脚本路径相同（$repoFull）：" +
            '自比恒真，证明不了运行副本是否与仓库一致 → undecidable')
    } elseif ($repoFacts.reason -ne 'ok') {
        $runReason = 'repo_script_' + $repoFacts.reason
        $runDetail = ("仓库脚本读不到（$($repoFacts.reason)：$($repoFacts.error)）：" +
            "$WatchdogScript —— 没有比较基准 → undecidable（读不到 ≠ 不一致）")
    } elseif ($runFacts.reason -ne 'ok') {
        $runReason = 'running_script_' + $runFacts.reason
        if ($runFacts.reason -eq 'missing') {
            $runDetail = ("运行副本不存在：$RunningScriptPath —— " +
                '没有可比较的运行副本 → undecidable（既不折成一致，也不折成不一致；' +
                'exists=false 是确定事实，matches_repo 仍是 null）')
        } else {
            $runDetail = ("运行副本读不到（$($runFacts.reason)：$($runFacts.error)）：" +
                "$RunningScriptPath —— 读不到 ≠ 与仓库不一致 → undecidable")
        }
    } elseif (Test-ByteArrayEqual $repoFacts.bytes $runFacts.bytes) {
        $runStatus = 'ok'
        $runReason = 'ok'
        $runMatches = $true
        $runDetail = ("运行副本与仓库脚本逐字节相同（sha256=" +
            "$($runFacts.sha256.Substring(0, 12))...，size=$($runFacts.size)）")
    } else {
        $runStatus = 'mismatch'
        $runReason = 'content_mismatch'
        $runMatches = $false
        $runDetail = ("运行副本与仓库脚本**不一致**（逐字节）：仓库 sha256=" +
            "$($repoFacts.sha256.Substring(0, 12))... size=$($repoFacts.size)；" +
            "运行副本 sha256=$($runFacts.sha256.Substring(0, 12))... " +
            "size=$($runFacts.size) —— 按不一致处理，先查清差异来源再决定是否进入窗口")
    }
    $runningCheck = [ordered]@{
        check = 'running_script_check'
        status = $runStatus
        path = $RunningScriptPath
        path_full = $runFull
        exists = $runFacts.exists
        size = $runFacts.size
        mtime = $runFacts.mtime
        mtime_utc_iso = $runFacts.mtime_iso
        mtime_reason = $runFacts.mtime_reason
        sha256 = $runFacts.sha256
        matches_repo = $runMatches
        reason = $runReason
        detail = $runDetail
        compare = 'byte_for_byte'
        repo_path = $WatchdogScript
        repo_path_full = $repoFull
        repo_exists = $repoFacts.exists
        repo_sha256 = $repoFacts.sha256
        not_proved = ('运行进程实际加载的代码、运行副本的 ACL/属主：' +
            '本节只比较磁盘上的字节')
    }
}

$nowUtc = [DateTimeOffset]::UtcNow
$elevated = ([Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name

# --------------------------------------------------------------- (1) watchdog 任务
# Same envelope as `share_chain_snapshot.query_watchdog_task` (elevated / name /
# found / state / stateText / error). The python side re-derives the signal from
# this envelope with its own `parse_watchdog_query` + `watchdog_task_signal`, so the
# two ends share one convention instead of two copies of the rules.
$wdFound = $false
$wdState = $null
$wdStateText = $null
$wdError = $null
try {
    $task = Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction Stop
    $wdFound = $true
    $wdState = [int]$task.State
    $wdStateText = [string]$task.State
} catch {
    $wdError = (Get-ErrorName $_) + '|' + $_.FullyQualifiedErrorId
}

$wdStatus = 'undecidable'
$wdReason = 'query_error'
$wdDetail = ''
if ($wdFound) {
    if ($wdState -is [int]) {
        $wdStatus = 'ok'
        $wdReason = 'ok'
        $wdDetail = "任务已注册：State=$wdStateText（$wdState）"
    } else {
        $wdReason = 'state_unavailable'
        $wdDetail = '任务对象可见但 State 读不到——读不到 ≠ 未注册'
    }
} elseif ($elevated -and $wdError -like '*NotFound*') {
    $wdStatus = 'absent'
    $wdReason = 'not_registered_elevated'
    $wdDetail = '提升身份下的权威读数：任务未注册（absent）'
} elseif (-not $elevated) {
    $wdReason = 'not_visible_to_current_identity'
    $wdDetail = ("非提升身份（$identity）查不到 SYSTEM 任务：按『读不到』记 " +
        "undecidable，绝不记成『未注册』（动作门 1.6）")
} else {
    $wdDetail = "任务查询返回错误而非权威的『未注册』：$wdError"
}

# Corroborating cross-check: `schtasks /query` (read-only). Its non-zero exit code
# is evidence of visibility, never of "not registered".
$schtasksRc = $null
$schtasksOk = $false
$schtasksStatusText = $null
$schtasksError = $null
try {
    $schOut = & schtasks /query /TN $WatchdogTaskName /FO LIST 2>&1
    $schtasksRc = $LASTEXITCODE
    $schtasksOk = ($schtasksRc -eq 0)
    if ($schtasksOk) {
        foreach ($line in $schOut) {
            if ($line -match '^\s*(Status|状态)\s*[:：]\s*(.+)$') {
                $schtasksStatusText = $Matches[2].Trim()
            }
        }
    } else {
        $schtasksError = (($schOut | Out-String).Trim())
        if ($schtasksError.Length -gt 300) {
            $schtasksError = $schtasksError.Substring(0, 300)
        }
    }
} catch {
    $schtasksError = $_.Exception.Message
}

# ------------------------------------------------------ (2) WFP + 防火墙 limits
# Reuse the watchdog's own read-only checks (dot-sourced with -FunctionsOnly: it
# defines the functions, creates nothing and starts no loop).
$limitsFnOk = $false
$limitsOk = $false
$limitReason = 'limits_function_missing'
$wfpRc = $null
$wfpText = ''
$firewallOk = $false
$firewallReason = 'not_evaluated'
$backendPort = $null
$backendPortError = ''
$limitsStatus = 'undecidable'
$limitsReason = 'limits_unavailable'
$limitsDetail = ''

if (-not (Test-PathQuiet $WatchdogScript)) {
    $limitsReason = 'watchdog_script_missing'
    $limitsDetail = "找不到看门狗脚本：$WatchdogScript"
} else {
    # The dot-sourced script's own top-level code (watchdog-umshare.ps1:70) runs
    # Test-Path on the SYSTEM-only backend-port.txt; its non-terminating
    # access-denied error goes to stream 2, which is silenced here so the probe's
    # own stderr stays readable. The reason is recorded in the JSON
    # (wfp_reason / backend_port_reason), so nothing is hidden.
    # Semantics unchanged: Get-Content / Get-FirewallState pass -ErrorAction Stop.
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        . $WatchdogScript -RunRoot $RunRoot -AppExe $AppExe -WfpHelper $WfpHelper `
            -FirewallRule $FirewallRule -FunctionsOnly 2>$null
        # The dot-sourced script sets $ErrorActionPreference = 'Continue' in THIS
        # scope; set it back to silent for our own read-only probes.
        $ErrorActionPreference = 'SilentlyContinue'
        $limitsFnOk = [bool](Get-Command Get-LimitsState -ErrorAction SilentlyContinue)
        $firewallFnOk = [bool](Get-Command Get-FirewallState -ErrorAction SilentlyContinue)
        if ($limitsFnOk) {
            $limitsOk, $limitReason = Get-LimitsState 2>$null
            $backendPort = $script:BackendPort
            $backendPortError = $script:BackendPortError
            if ($limitsOk -and $firewallFnOk) {
                # Same order as the watchdog loop (watchdog-umshare.ps1:410): the
                # firewall half is only consulted when the WFP half is ok.
                $firewallOk, $firewallReason = Get-FirewallState 2>$null
            }
        }
    } catch {
        $limitReason = 'limits_probe_error:' + (Get-ErrorName $_)
        $limitsDetail = $_.Exception.Message
    }
}

if (-not $limitsFnOk) {
    # The watchdog script no longer defines Get-LimitsState: the whole limits
    # half CANNOT be read -> undecidable (never a definite "not in place").
    if ($limitsReason -notlike 'limits_probe_error*') {
        $limitsReason = 'limits_function_missing'
        $limitsDetail = ("看门狗脚本里没有 Get-LimitsState（$WatchdogScript）：" +
            'limits 半场读不到 → undecidable（读不到 ≠ limits 在位）')
    }
} else {
    # Raw evidence text: the SAME helper with the SAME arguments the watchdog
    # passes (read-only query; it never adds or removes a filter).
    if ($backendPort -ge 1 -and (Test-PathQuiet $WfpHelper)) {
        try {
            $wfpOut = & pwsh -NoProfile -ExecutionPolicy Bypass `
                -File $WfpHelper -Action strong -AppExePath $AppExe `
                -Port $backendPort 2>&1
            $wfpRc = $LASTEXITCODE
            $wfpText = (($wfpOut | Out-String).Trim())
            if ($wfpText.Length -gt 500) {
                $wfpText = $wfpText.Substring(0, 500)
            }
        } catch {
            $wfpText = 'wfp_helper 调用异常：' + $_.Exception.Message
        }
    }
    if ($limitsOk -and $firewallOk) {
        $limitsStatus = 'ok'
        $limitsReason = 'ok'
        $limitsDetail = 'WFP strong=OK（rc/文本见 wfp_detail）；出站阻断规则 ok'
    } elseif (-not $firewallFnOk) {
        # The WFP half is ok but the watchdog script no longer defines
        # Get-FirewallState: the firewall half CANNOT be read -> undecidable.
        # (Never "absent": a missing function is not a verified "rule gone".)
        $limitsStatus = 'undecidable'
        $limitsReason = 'firewall_function_missing'
        $limitsDetail = ("看门狗脚本里没有 Get-FirewallState（$WatchdogScript）：" +
            '防火墙半场读不到 → undecidable（读不到 ≠ 规则不在位）')
    } elseif ($limitReason -like 'detection-failure*') {
        # The helper could not run / no permit port could be read: UNREADABLE.
        # (The watchdog treats this as LIMIT-LOSS; a snapshot must not.)
        $limitsReason = 'detection_failure'
        $limitsDetail = "limits 读不到（$limitReason）：读不到 ≠ limits 在位"
    } elseif ($limitReason -like 'strong-not-ok*') {
        $limitsStatus = 'absent'
        $limitsReason = 'wfp_not_in_place'
        $limitsDetail = "WFP strong 检查未通过（$limitReason）"
    } elseif ($limitReason -eq 'ok' -and
              ($firewallReason -like 'firewall-error*' -or
               $firewallReason -like 'scope-query-error*')) {
        $limitsReason = 'firewall_detection_failure'
        $limitsDetail = "防火墙判定读不到（$firewallReason）：读不到 ≠ 规则不在"
    } elseif ($limitReason -eq 'ok') {
        $limitsStatus = 'absent'
        $limitsReason = 'firewall_not_in_place'
        $limitsDetail = "出站阻断规则不在位（$firewallReason）"
    } else {
        $limitsReason = 'limits_reason_unrecognized:' + $limitReason
        $limitsDetail = "limits 判定返回未知原因（$limitReason）：不折成『在位』也不折成『不在位』"
    }
}

# ------------------------------------------------------------- (3) raw.txt 日志
$rawExists = $null
$rawSize = $null
$rawMtime = $null
$rawMtimeIso = $null
$rawMtimeReason = 'not_read'
$rawStatus = 'undecidable'
$rawReason = 'raw_txt_unreadable'
$rawDetail = ''
try {
    $rawItem = Get-Item -LiteralPath $RawTxtPath -ErrorAction Stop
    $rawExists = $true
    $rawSize = $rawItem.Length
    $rawMtimeFacts = Get-MtimeFacts $rawItem
    $rawMtime = $rawMtimeFacts.mtime
    $rawMtimeIso = $rawMtimeFacts.mtime_iso
    $rawMtimeReason = $rawMtimeFacts.mtime_reason
    $rawStatus = 'ok'
    $rawReason = 'ok'
    $rawDetail = "raw.txt 存在：size=$rawSize mtime=$rawMtimeIso（内容不读）"
} catch {
    $rawName = Get-ErrorName $_
    if ($rawName -eq 'ItemNotFoundException') {
        $rawExists = $false
        $rawStatus = 'absent'
        $rawReason = 'raw_txt_absent'
        $rawDetail = "raw.txt 不存在：$RawTxtPath"
    } else {
        $rawDetail = "raw.txt 读不到（$rawName）：$RawTxtPath（读不到 ≠ 不存在）"
    }
}

# --------------------------------------------------------- (4) backend.pid 字节
# Path rule = `console.share_production._backend_dir()` (env override must be an
# absolute path; otherwise LOCALAPPDATA\SelfUseAI\ai-gateway\share-backend).
$backendDirResolved = ''
$backendDirSource = ''
if ($BackendDir -ne '') {
    $backendDirResolved = $BackendDir
    $backendDirSource = 'explicit_parameter'
} elseif ($env:CONSOLE_SHARE_BACKEND_DIR) {
    if ([System.IO.Path]::IsPathRooted($env:CONSOLE_SHARE_BACKEND_DIR)) {
        $backendDirResolved = $env:CONSOLE_SHARE_BACKEND_DIR
        $backendDirSource = 'env:CONSOLE_SHARE_BACKEND_DIR'
    } else {
        $backendDirSource = 'env:CONSOLE_SHARE_BACKEND_DIR_relative(undecidable)'
    }
} elseif ($env:LOCALAPPDATA) {
    $backendDirResolved = Join-Path $env:LOCALAPPDATA 'SelfUseAI\ai-gateway\share-backend'
    $backendDirSource = 'LOCALAPPDATA_default'
} else {
    $backendDirSource = 'localappdata_unavailable'
}

$pidStatus = 'undecidable'
$pidReason = 'backend_dir_unresolved'
$pidDetail = ''
$pidPath = $null
$pidContent = $null
$pidBytesHex = $null
$pidSize = $null
$pidValue = $null
$pidMtime = $null
$pidMtimeIso = $null
$pidMtimeReason = 'not_read'
if ($backendDirResolved -ne '') {
    $pidPath = Join-Path $backendDirResolved 'backend.pid'
    try {
        $pidItem = Get-Item -LiteralPath $pidPath -ErrorAction Stop
        $pidBytes = [System.IO.File]::ReadAllBytes($pidPath)
        $pidSize = $pidBytes.Length
        $pidBytesHex = [System.BitConverter]::ToString($pidBytes).Replace('-', '')
        $pidContent = [System.Text.Encoding]::ASCII.GetString($pidBytes)
        $pidMtimeFacts = Get-MtimeFacts $pidItem
        $pidMtime = $pidMtimeFacts.mtime
        $pidMtimeIso = $pidMtimeFacts.mtime_iso
        $pidMtimeReason = $pidMtimeFacts.mtime_reason
        $pidStatus = 'ok'
        $pidTrimmed = $pidContent.Trim()
        if ($pidTrimmed -match '^\d+$') {
            $pidValue = [int]$pidTrimmed
            $pidReason = 'ok'
            $pidDetail = "backend.pid 字节=$pidSize 内容=$pidTrimmed（PID）mtime=$pidMtimeIso"
        } else {
            $pidReason = 'pid_content_not_an_integer'
            $pidDetail = "backend.pid 字节=$pidSize 但内容不是整数 PID（mtime=$pidMtimeIso）"
        }
    } catch {
        $pidName = Get-ErrorName $_
        if ($pidName -eq 'ItemNotFoundException') {
            $pidStatus = 'absent'
            $pidReason = 'pid_file_absent'
            $pidDetail = "backend.pid 不存在：$pidPath"
        } else {
            $pidReason = 'pid_file_unreadable:' + $pidName
            $pidDetail = "backend.pid 读不到（$pidName）：$pidPath（读不到 ≠ 不存在）"
        }
    }
} else {
    $pidDetail = "后端目录无法解析（$backendDirSource）"
}

# ------------------------------------------------------------------- 输出
$signals = [ordered]@{
    watchdog_task = [ordered]@{
        signal = 'watchdog_task'
        status = $wdStatus
        task_name = $WatchdogTaskName
        state = $wdState
        state_text = $wdStateText
        elevated = $elevated
        identity = $identity
        reason = $wdReason
        detail = $wdDetail
        # Raw envelope (same keys as query_watchdog_task): the python merge
        # re-derives the signal from it with its own parse + rules.
        query = [ordered]@{
            elevated = $elevated
            name = $identity
            found = $wdFound
            state = $wdState
            stateText = $wdStateText
            error = $wdError
        }
        schtasks_rc = $schtasksRc
        schtasks_ok = $schtasksOk
        schtasks_status_text = $schtasksStatusText
        schtasks_error = $schtasksError
    }
    wfp_limits = [ordered]@{
        signal = 'wfp_limits'
        status = $limitsStatus
        app_exe = $AppExe
        backend_port = $backendPort
        backend_port_reason = $backendPortError
        helper = $WfpHelper
        helper_present = $(if (Test-PathQuiet $WfpHelper) { $true } else { $false })
        limits_source = 'um-share-deploy/watchdog-umshare.ps1:Get-LimitsState/Get-FirewallState'
        wfp_ok = $limitsOk
        wfp_reason = $limitReason
        wfp_rc = $wfpRc
        wfp_detail = $wfpText
        firewall_ok = $firewallOk
        firewall_reason = $firewallReason
        firewall_rule = $FirewallRule
        reason = $limitsReason
        detail = $limitsDetail
    }
    raw_txt = [ordered]@{
        signal = 'raw_txt'
        status = $rawStatus
        path = $RawTxtPath
        exists = $rawExists
        size = $rawSize
        mtime = $rawMtime
        mtime_utc_iso = $rawMtimeIso
        mtime_reason = $rawMtimeReason
        reason = $rawReason
        detail = $rawDetail
    }
    backend_pid_file = [ordered]@{
        signal = 'backend_pid_file'
        status = $pidStatus
        path = $pidPath
        path_source = $backendDirSource
        backend_dir = $backendDirResolved
        content = $pidContent
        bytes_hex = $pidBytesHex
        size = $pidSize
        pid = $pidValue
        mtime = $pidMtime
        mtime_utc_iso = $pidMtimeIso
        mtime_reason = $pidMtimeReason
        reason = $pidReason
        detail = $pidDetail
    }
}

# The single explicit write path: -Out <path> (UTF-8 without BOM). Resolved BEFORE
# the payload so `write_actions` reports it truthfully; without -Out nothing is
# written and `write_actions` stays empty.
$writeActions = @()
$fullOut = ''
if ($Out -ne '') {
    $fullOut = [System.IO.Path]::GetFullPath($Out)
    $parent = Split-Path -Parent $fullOut
    if ($parent -and -not (Test-PathQuiet $parent)) {
        Write-Error "输出目录不存在：$parent"
        exit 2
    }
    $writeActions += ('-Out -> ' + $fullOut)
}

$payload = [ordered]@{
    tool = 'authoritative_readings'
    schema_version = 1
    read_only = $true
    write_actions = @($writeActions)
    elevated = $elevated
    identity = $identity
    probe_pid = $PID
    captured_at = Get-UtcEpoch $nowUtc.UtcDateTime
    captured_at_iso = Get-IsoOffset $nowUtc
    run_root = $RunRoot
    watchdog_script = $WatchdogScript
    deploy_dir = $DeployDir
    window_signals = $signals
    not_covered = @(
        '8096 读面键集 / 分享入口 404/401 / 账本 / Funnel：由非提升快照（share_chain_snapshot.py）采集，本探针不重复',
        'raw.txt 内容不读（只记路径、size 与 mtime）',
        '密钥内容与任何分享 Key 明文：本探针不读密钥文件，也不打印令牌',
        '本探针不写任何文件：默认只打印到 stdout；唯一写路径是显式 -Out（本轮未执行）'
    )
}

# `running_script_check` 只在 `-RunningScriptPath` 给定时存在：不给该参数时输出与加此
# 能力之前逐字相同（不新增键、不改任何既有值）。
if ($runCheckEnabled) {
    $payload['running_script_check'] = $runningCheck
}

$json = $payload | ConvertTo-Json -Depth 8
if ($fullOut -ne '') {
    # UTF-8 WITHOUT BOM: PowerShell 5.1 `>` writes UTF-16LE, which python's
    # json.load cannot read back.
    [System.IO.File]::WriteAllText($fullOut, $json + "`n",
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "authoritative_readings: 已写出 $fullOut"
} else {
    Write-Output $json
}
exit 0
