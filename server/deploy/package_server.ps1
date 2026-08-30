[CmdletBinding()]
param(
    [string]$ReleaseId = 'clearmeeting-server-v2.0.0-stable-r1',
    [string]$OutputDir = 'releases',
    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$output = [IO.Path]::GetFullPath((Join-Path $root $OutputDir))
$zipPath = Join-Path $output "$ReleaseId.zip"
$zipHashPath = "$zipPath.sha256"

function Get-Sha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-RelativePath([string]$Base, [string]$Path) {
    $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes release root: $pathFull"
    }
    $pathFull.Substring($baseFull.Length).Replace('\', '/')
}

function Copy-ReleaseTree([string]$SourceRelative, [string]$StageRoot) {
    $source = Join-Path $root $SourceRelative
    if (-not (Test-Path -LiteralPath $source)) { throw "Missing release source: $source" }
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        $destination = Join-Path $StageRoot $SourceRelative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
        return
    }

    $sourcePrefix = [IO.Path]::GetFullPath($source).TrimEnd('\', '/') + '\'
    foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
        $full = [IO.Path]::GetFullPath($file.FullName)
        $within = $full.Substring($sourcePrefix.Length).Replace('\', '/')
        if ($within -match '(^|/)(__pycache__|node_modules|\.pytest_cache|\.venv[^/]*)(/|$)' -or
            $within -match '(^|/)data(/|$)' -or
            $within -match '(^|/)\.env$' -or
            $within -match '\.(pyc|pyo)$') {
            continue
        }
        $destination = Join-Path $StageRoot (Join-Path $SourceRelative $within)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
}

New-Item -ItemType Directory -Force -Path $output | Out-Null

# Recover safely from an interrupted packaging run.  Only tool-owned staging
# directories below the resolved release directory and an archive without its
# companion checksum are considered incomplete.
$safeOutputPrefix = $output.TrimEnd('\', '/') + '\'
foreach ($stale in Get-ChildItem -LiteralPath $output -Directory -Force |
         Where-Object { $_.Name -like '.staging-*' -or $_.Name -like '.verify-*' }) {
    $resolvedStale = [IO.Path]::GetFullPath($stale.FullName)
    if (-not $resolvedStale.StartsWith($safeOutputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe stale package path: $resolvedStale"
    }
    Remove-Item -LiteralPath $resolvedStale -Recurse -Force
}
if ((Test-Path -LiteralPath $zipPath -PathType Leaf) -and
    -not (Test-Path -LiteralPath $zipHashPath -PathType Leaf)) {
    $resolvedPartial = [IO.Path]::GetFullPath($zipPath)
    if (-not $resolvedPartial.StartsWith($safeOutputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe partial archive path: $resolvedPartial"
    }
    Remove-Item -LiteralPath $resolvedPartial -Force
}
if ($ReplaceExisting) {
    foreach ($existing in @($zipPath, $zipHashPath)) {
        if (-not (Test-Path -LiteralPath $existing -PathType Leaf)) { continue }
        $resolvedExisting = [IO.Path]::GetFullPath($existing)
        if (-not $resolvedExisting.StartsWith($safeOutputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe existing archive path: $resolvedExisting"
        }
        Remove-Item -LiteralPath $resolvedExisting -Force
    }
}
foreach ($target in @($zipPath, $zipHashPath)) {
    if (Test-Path -LiteralPath $target) { throw "Refusing to overwrite: $target" }
}

$stage = Join-Path $output ('.staging-' + [guid]::NewGuid().ToString('N'))
$verify = Join-Path $output ('.verify-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$complete = $false

try {
    foreach ($entry in @(
        'README.md',
        'package.json',
        'package-lock.json',
        'server',
        'apps/web-client',
        'apps/card-sim',
        'deploy',
        'docs/LUOYE_DEVICE_API_V2.md',
        'docs/DEPLOYMENT-V0.15.0.md',
        'docs/DEPLOYMENT-V0.17.0.md',
        'docs/DEPLOYMENT-V0.18.0.md',
        'docs/DEPLOYMENT-V0.19.0.md',
        'docs/DEPLOYMENT-V0.19.1.md',
        'docs/DEPLOYMENT-V0.19.2.md',
        'docs/DEPLOYMENT-V0.19.3.md',
        'docs/DEPLOYMENT-V0.20.0.md',
        'docs/DEPLOYMENT-V0.20.1.md',
        'docs/DEPLOYMENT-V0.21.0.md',
        'docs/DEPLOYMENT-V2.0.0.md'
    )) {
        Copy-ReleaseTree $entry $stage
    }

    $manifestFiles = @(Get-ChildItem -LiteralPath $stage -File -Recurse | ForEach-Object {
        [ordered]@{
            path = Get-RelativePath $stage $_.FullName
            bytes = $_.Length
            sha256 = Get-Sha256 $_.FullName
        }
    } | Sort-Object path)
    $manifest = [ordered]@{
        schema = 1
        release_id = $ReleaseId
        created_utc = [DateTime]::UtcNow.ToString('o')
        product = 'ClearMeeting'
        server_version = '2.0.0'
        api_contract = 'luoye-device-api/2'
        device_auth_profile = 'engineering'
        minimum_firmware = '0.9.3'
        persistent_data_included = $false
        deploy_env_included = $false
        files = $manifestFiles
    }
    $manifest | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath (Join-Path $stage 'RELEASE-MANIFEST.json') -Encoding utf8

    $sumRows = Get-ChildItem -LiteralPath $stage -File -Recurse |
        Where-Object Name -ne 'SHA256SUMS.txt' |
        ForEach-Object { "$(Get-Sha256 $_.FullName)  $(Get-RelativePath $stage $_.FullName)" } |
        Sort-Object
    [IO.File]::WriteAllText(
        (Join-Path $stage 'SHA256SUMS.txt'),
        (($sumRows -join "`n") + "`n"),
        [Text.UTF8Encoding]::new($false))

    & python (Join-Path $PSScriptRoot 'create_portable_zip.py') --source $stage --output $zipPath
    if ($LASTEXITCODE -ne 0) { throw "Portable ZIP creation failed with exit code $LASTEXITCODE" }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $verify
    foreach ($line in Get-Content -LiteralPath (Join-Path $verify 'SHA256SUMS.txt')) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "Invalid checksum row: $line" }
        $file = Join-Path $verify $Matches[2]
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Archive missing: $($Matches[2])" }
        if ((Get-Sha256 $file) -ne $Matches[1]) { throw "Checksum mismatch: $($Matches[2])" }
    }
    $zipHash = Get-Sha256 $zipPath
    # Linux `sha256sum -c` treats a CR before the filename terminator as part
    # of the filename.  Write an explicit LF-only companion file so the
    # package can be verified unchanged on Windows and Linux.
    [IO.File]::WriteAllText(
        $zipHashPath,
        "$zipHash  $([IO.Path]::GetFileName($zipPath))`n",
        [Text.UTF8Encoding]::new($false))
    $complete = $true
}
finally {
    $safePrefix = $output.TrimEnd('\', '/') + '\'
    foreach ($temporary in @($stage, $verify)) {
        $resolved = [IO.Path]::GetFullPath($temporary)
        if ($resolved.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $resolved)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    if (-not $complete) {
        foreach ($partial in @($zipPath, $zipHashPath)) {
            $resolved = [IO.Path]::GetFullPath($partial)
            if ($resolved.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase) -and
                (Test-Path -LiteralPath $resolved -PathType Leaf)) {
                Remove-Item -LiteralPath $resolved -Force
            }
        }
    }
}

[pscustomobject]@{
    ReleaseId = $ReleaseId
    Package = $zipPath
    Sha256 = Get-Sha256 $zipPath
    Bytes = (Get-Item -LiteralPath $zipPath).Length
}
