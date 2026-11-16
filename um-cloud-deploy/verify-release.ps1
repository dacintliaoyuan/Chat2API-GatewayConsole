# Windows-side verifier for an unpacked UM cloud release payload.
#
# This script is a QA gate only: it reads the manifest, hashes the packaged
# bytes and checks the required deployment files.  It never starts a service,
# never writes a production dist and never touches a data directory.
#
#   pwsh -NoProfile -File verify-release.ps1 -PackageRoot <payload> [-WebRole cloud|local] [-WebLine production]
param(
    [string]$PackageRoot = $PSScriptRoot,
    [string]$ManifestPath = '',
    [string]$WebRole = '',
    [string]$WebLine = 'production'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PackageRoot).Path
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $manifestPath = Join-Path $root 'RELEASE-MANIFEST.json'
}
else {
    $manifestPath = (Resolve-Path -LiteralPath $ManifestPath).Path
}
$lockPath = Join-Path $root 'um-cloud-deploy/dependencies.lock.json'
$errors = [System.Collections.Generic.List[string]]::new()

function Add-Error([string]$message) {
    $script:errors.Add($message)
}

function Test-FileHash([string]$relativePath, [string]$expected, [string]$kind) {
    $path = Join-Path $root $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-Error "$kind missing: $relativePath"
        return
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected.ToLowerInvariant()) {
        Add-Error "$kind sha256 mismatch: $relativePath"
    }
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Write-Error "RELEASE-MANIFEST.json missing under $root"
    exit 1
}
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    Write-Error "dependencies.lock.json missing under $root"
    exit 1
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json

if ($manifest.schema_version -ne 2) {
    Add-Error "unexpected manifest schema_version: $($manifest.schema_version)"
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.release_version)) {
    Add-Error 'release_version is empty'
}
if ($null -eq $manifest.files -or $manifest.files.Count -eq 0) {
    Add-Error 'files is empty'
}
if ($null -eq $manifest.deployment_files -or $manifest.deployment_files.Count -eq 0) {
    Add-Error 'deployment_files is empty'
}
if ($manifest.python.target -ne $lock.target.python) {
    Add-Error "manifest python.target=$($manifest.python.target) does not match lock target=$($lock.target.python)"
}

foreach ($entry in @($manifest.files)) {
    Test-FileHash -relativePath $entry.path -expected $entry.sha256 -kind 'file'
}
foreach ($entry in @($manifest.deployment_files)) {
    Test-FileHash -relativePath $entry.path -expected $entry.sha256 -kind 'deployment'
}

if ($manifest.web_dist.included -eq $true) {
    if ($null -eq $manifest.web_dist.files -or $manifest.web_dist.files.Count -eq 0) {
        Add-Error 'web_dist.included=true but web_dist.files is empty'
    }
    foreach ($entry in @($manifest.web_dist.files)) {
        Test-FileHash -relativePath $entry.path -expected $entry.sha256 -kind 'web'
    }
    # The carried tree's root comes from the manifest: a payload carries it as
    # web/dist, a source manifest may name the candidate it was built from.
    $webRoot = [string]$manifest.web_dist.root
    if ([string]::IsNullOrWhiteSpace($webRoot)) { $webRoot = 'web/dist' }
    $index = Join-Path $root (Join-Path $webRoot 'index.html')
    if (-not (Test-Path -LiteralPath $index -PathType Leaf)) {
        Add-Error "web_dist.included=true but $webRoot/index.html is missing"
    }
    # Bind the carried bytes to the build identity: the role and line it was
    # built as, and the module script its index.html loads, must agree with the
    # manifest.  usagesrv.py refuses any line other than production, so a
    # release payload must not carry a candidate tree.
    $buildRole = [string]$manifest.web_dist.build.role
    $buildLine = [string]$manifest.web_dist.build.line
    if (-not [string]::IsNullOrWhiteSpace($WebRole) -and $buildRole -ne $WebRole) {
        Add-Error "web_dist.build.role=$buildRole does not match -WebRole=$WebRole"
    }
    if ([string]::IsNullOrWhiteSpace($buildLine)) {
        Add-Error 'web_dist.build.line is empty; a release payload must carry a production console build'
    }
    elseif ($buildLine -ne $WebLine) {
        Add-Error "web_dist.build.line=$buildLine does not match -WebLine=$WebLine; usagesrv.py refuses a non-production line"
    }
    $buildFile = Join-Path $root (Join-Path $webRoot 'candidate-build.json')
    if (-not [string]::IsNullOrWhiteSpace($buildRole)) {
        if (-not (Test-Path -LiteralPath $buildFile -PathType Leaf)) {
            Add-Error "web_dist.build.role=$buildRole but $webRoot/candidate-build.json is missing"
        }
        else {
            $carriedBuild = Get-Content -LiteralPath $buildFile -Raw | ConvertFrom-Json
            if ([string]$carriedBuild.role -ne $buildRole) {
                Add-Error "carried candidate-build.json role=$($carriedBuild.role) does not match web_dist.build.role=$buildRole"
            }
            if ([string]$carriedBuild.line -ne $buildLine) {
                Add-Error "carried candidate-build.json line=$($carriedBuild.line) does not match web_dist.build.line=$buildLine"
            }
        }
    }
    $entryPath = [string]$manifest.web_dist.entry
    if (-not [string]::IsNullOrWhiteSpace($entryPath)) {
        $entryFile = Join-Path $root (Join-Path $webRoot $entryPath)
        if (-not (Test-Path -LiteralPath $entryFile -PathType Leaf)) {
            Add-Error "web_dist.entry=$entryPath is missing from the carried tree"
        }
        elseif (Test-Path -LiteralPath $index -PathType Leaf) {
            $html = Get-Content -LiteralPath $index -Raw
            if ($html -notmatch [regex]::Escape("/$entryPath")) {
                Add-Error "$webRoot/index.html does not load the recorded entry $entryPath"
            }
        }
    }
}
elseif ($manifest.web_dist.included -eq $false) {
    if ([string]::IsNullOrWhiteSpace([string]$manifest.web_dist.reason)) {
        Add-Error 'web_dist.included=false requires an explicit reason'
    }
}
else {
    Add-Error 'web_dist.included must be an explicit boolean'
}

$runtime = [string]$manifest.runtime
if ([string]::IsNullOrWhiteSpace($runtime)) { $runtime = 'python' }
if ($runtime -notin @('python', 'rust')) {
    Add-Error "unexpected manifest runtime: $runtime"
}
$requiredFiles = @(
    'um-cloud-deploy/install.sh',
    'um-cloud-deploy/update.sh',
    'um-cloud-deploy/rollback.sh',
    'um-cloud-deploy/rollback-drill.sh',
    'um-cloud-deploy/package-release.sh',
    'um-cloud-deploy/bin/um-share-brokerctl.py',
    'um-cloud-deploy/systemd/um-share-console.service',
    'um-cloud-deploy/Caddyfile.example',
    'um-cloud-deploy/requirements.lock'
)
if ($runtime -eq 'rust') {
    $requiredFiles += 'um-cloud-deploy/bin/um-gateway'
    $requiredFiles += 'um-cloud-deploy/systemd/um-share-gateway.service'
}
else {
    $requiredFiles += 'um-cloud-deploy/systemd/um-share-broker.service'
}
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $required) -PathType Leaf)) {
        Add-Error "required deployment file missing: $required"
    }
}

if ($errors.Count -gt 0) {
    Write-Host "release verification FAILED ($($errors.Count) issue(s))"
    foreach ($item in $errors) {
        Write-Host " - $item"
    }
    exit 1
}

Write-Host "release verification OK: version=$($manifest.release_version) files=$($manifest.files.Count) deployment=$($manifest.deployment_files.Count) web_dist=$($manifest.web_dist.included)"
exit 0
