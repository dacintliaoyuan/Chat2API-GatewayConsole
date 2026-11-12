# Controlled test runtime wrapper for the local composition root (E19-1-04).
#
# ``runtime_control.mode=powershell`` must point at a script INSIDE the package,
# and that script must never start or stop a real service. This fixture only
# echoes the requested action as JSON so ``PowerShellServiceRuntime`` can confirm
# the call round-trips. It deliberately carries none of the production start/stop
# markers the config guard refuses (see FORBIDDEN_RUNTIME_CONTENT in
# ``local/backend/config.py``): no process spawn, no console-runtime start verb,
# and no dot-source of the gateway runtime library.
param(
  [Parameter(Mandatory = $false)]
  [ValidateSet('start', 'stop', 'status')]
  [string]$Action = 'status'
)

[pscustomobject]@{
  ok = $true
  action = $Action
  runtime = 'fixture'
} | ConvertTo-Json -Compress
