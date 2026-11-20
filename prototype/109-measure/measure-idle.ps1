param(
  [Parameter(Mandatory = $true)][int]$ProcessId,
  [int]$Seconds = 60,
  [string]$OutFile = "tmp/109-idle.json"
)

$ErrorActionPreference = "Stop"

function Get-ProcessTree([int]$root) {
  $all = Get-CimInstance Win32_Process
  $ids = [System.Collections.Generic.List[int]]::new()
  $ids.Add($root)
  $frontier = @($root)
  while ($frontier.Count -gt 0) {
    $next = @()
    foreach ($parent in $frontier) {
      foreach ($child in ($all | Where-Object { $_.ParentProcessId -eq $parent })) {
        $childId = [int]$child.ProcessId
        if (-not $ids.Contains($childId)) {
          $ids.Add($childId)
          $next += $childId
        }
      }
    }
    $frontier = $next
  }
  return $ids
}

function Get-Snapshot([int[]]$ids) {
  $rows = @{}
  foreach ($id in $ids) {
    $process = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    $rows[$id] = @{
      name = $process.ProcessName
      cpu_s = $process.TotalProcessorTime.TotalSeconds
      working_set = $process.WorkingSet64
      private = $process.PrivateMemorySize64
    }
  }
  return $rows
}

$ids = Get-ProcessTree $ProcessId
$start = Get-Snapshot $ids
$watch = [System.Diagnostics.Stopwatch]::StartNew()
Start-Sleep -Seconds $Seconds
$watch.Stop()
$end = Get-Snapshot $ids

$elapsed = $watch.Elapsed.TotalSeconds
$logical = [Environment]::ProcessorCount
$rows = @()
$totalCpu = 0.0
$totalWorking = 0L
$totalPrivate = 0L
$webviewCpu = 0.0
$webviewWorking = 0L
$webviewPrivate = 0L
foreach ($id in $end.Keys) {
  $after = $end[$id]
  $before = $start[$id]
  $delta = if ($before) { [double]$after.cpu_s - [double]$before.cpu_s } else { 0.0 }
  $oneCore = if ($elapsed -gt 0) { $delta / $elapsed * 100.0 } else { 0.0 }
  $allCores = if ($elapsed -gt 0) { $oneCore / $logical } else { 0.0 }
  $rows += [pscustomobject]@{
    pid = $id
    name = $after.name
    cpu_delta_s = [math]::Round($delta, 4)
    cpu_pct_one_core = [math]::Round($oneCore, 3)
    cpu_pct_all_cores = [math]::Round($allCores, 3)
    working_set_mb = [math]::Round($after.working_set / 1MB, 2)
    private_mb = [math]::Round($after.private / 1MB, 2)
  }
  $totalCpu += $delta
  $totalWorking += [long]$after.working_set
  $totalPrivate += [long]$after.private
  if ($after.name -eq "msedgewebview2") {
    $webviewCpu += $delta
    $webviewWorking += [long]$after.working_set
    $webviewPrivate += [long]$after.private
  }
}

$report = [pscustomobject]@{
  root_pid = $ProcessId
  elapsed_s = [math]::Round($elapsed, 3)
  logical_processors = $logical
  processes = $rows
  total = [pscustomobject]@{
    cpu_delta_s = [math]::Round($totalCpu, 4)
    cpu_pct_one_core = if ($elapsed -gt 0) { [math]::Round($totalCpu / $elapsed * 100.0, 3) } else { 0 }
    cpu_pct_all_cores = if ($elapsed -gt 0) { [math]::Round($totalCpu / $elapsed / $logical * 100.0, 3) } else { 0 }
    working_set_mb = [math]::Round($totalWorking / 1MB, 2)
    private_mb = [math]::Round($totalPrivate / 1MB, 2)
  }
  webview2 = [pscustomobject]@{
    processes = @($rows | Where-Object { $_.name -eq "msedgewebview2" }).Count
    cpu_delta_s = [math]::Round($webviewCpu, 4)
    working_set_mb = [math]::Round($webviewWorking / 1MB, 2)
    private_mb = [math]::Round($webviewPrivate / 1MB, 2)
  }
}

$parent = Split-Path -Parent $OutFile
if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
$report | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
$report | ConvertTo-Json -Depth 6
