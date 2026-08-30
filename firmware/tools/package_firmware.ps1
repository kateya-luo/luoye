[CmdletBinding()]
param(
    [string]$BuildDir = 'build-v171',
    [string]$ReleaseId = 'luoye-fw-v1.7.1-engineering-wav-dma-r1',
    [string]$ExpectedVersion = '1.7.1',
    [ValidateSet('dev', 'rc', 'release', 'engineering')]
    [string]$Profile = 'engineering',
    [string]$HardwareRev = 'LY-HW-ENG-20260710',
    [string]$ApiContract = 'luoye-device-api/2',
    [string]$ServerRelease = '0.21.0',
    [string]$MinimumClientVersion = '0.21.0',
    [ValidatePattern('^https?://[A-Za-z0-9.-]+(:[0-9]+)?$')]
    [string]$ServerBaseUrl = 'https://clearmeeting.chat',
    [switch]$AllowInsecureHttp,
    [string]$OutputDir = 'releases',
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$build = [IO.Path]::GetFullPath((Join-Path $project $BuildDir))
$output = [IO.Path]::GetFullPath((Join-Path $project $OutputDir))
$releasePattern = '^luoye-fw-v' + [regex]::Escape($ExpectedVersion) + '-' +
                  [regex]::Escape($Profile) + '-[a-z0-9]+(?:-[a-z0-9]+)*$'
if ($ReleaseId -notmatch $releasePattern) {
    throw "ReleaseId '$ReleaseId' does not identify firmware v$ExpectedVersion / $Profile"
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-PathRelativeToRoot([string]$Root, [string]$Path) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes checksum root: $pathFull"
    }
    $pathFull.Substring($rootPrefix.Length).Replace('\', '/')
}

function Read-GeneratedString([string]$Header, [string]$Macro) {
    $content = Get-Content -LiteralPath $Header -Raw -Encoding utf8
    $pattern = '(?m)^#define\s+' + [regex]::Escape($Macro) + '\s+"([^"]*)"\r?$'
    $match = [regex]::Match($content, $pattern)
    if (-not $match.Success) { throw "Missing $Macro in $Header" }
    $match.Groups[1].Value
}

function Read-GeneratedInt([string]$Header, [string]$Macro) {
    $content = Get-Content -LiteralPath $Header -Raw -Encoding utf8
    $pattern = '(?m)^#define\s+' + [regex]::Escape($Macro) + '\s+([0-9]+)\r?$'
    $match = [regex]::Match($content, $pattern)
    if (-not $match.Success) { throw "Missing $Macro in $Header" }
    [int]$match.Groups[1].Value
}

function Write-Checksums([string]$Root) {
    $rows = Get-ChildItem -LiteralPath $Root -File -Recurse |
        Where-Object Name -ne 'SHA256SUMS.txt' |
        ForEach-Object {
            $relative = Get-PathRelativeToRoot $Root $_.FullName
            "$(Get-Sha256 $_.FullName)  $relative"
        } |
        Sort-Object
    Set-Content -LiteralPath (Join-Path $Root 'SHA256SUMS.txt') -Value $rows -Encoding utf8
}

function Test-Checksums([string]$Root) {
    $sumFile = Join-Path $Root 'SHA256SUMS.txt'
    foreach ($line in Get-Content -LiteralPath $sumFile -Encoding utf8) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "Invalid checksum row: $line" }
        $path = Join-Path $Root $Matches[2]
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Archive missing: $($Matches[2])" }
        if ((Get-Sha256 $path) -ne $Matches[1]) { throw "Archive hash mismatch: $($Matches[2])" }
    }
}

if (-not (Test-Path -LiteralPath $build -PathType Container)) {
    throw "Build directory not found: $build"
}
if (-not (Test-Path -LiteralPath (Join-Path $project '.git'))) {
    throw 'Release packaging requires a Git repository.'
}

$commit = (& git -C $project rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve source commit.' }
$dirtyRows = @(& git -C $project status --porcelain)
$dirty = $dirtyRows.Count -gt 0
if ($dirty -and -not $AllowDirty) {
    throw 'Working tree is dirty. Commit the exact source before packaging.'
}

$descriptionPath = Join-Path $build 'project_description.json'
$flasherPath = Join-Path $build 'flasher_args.json'
$generatedHeader = Join-Path $build 'esp-idf\main\generated\luoye_build_config.h'
$generatedNetHeader = Join-Path $build 'esp-idf\net_uploader\generated\luoye_net_config.h'
foreach ($required in @($descriptionPath, $flasherPath, $generatedHeader,
                         $generatedNetHeader)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing build metadata: $required" }
}

$description = Get-Content -LiteralPath $descriptionPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($description.project_name -ne 'recorder_card') {
    throw "Unexpected project: $($description.project_name)"
}
if ($description.project_version -ne $ExpectedVersion) {
    throw "Embedded version '$($description.project_version)' != '$ExpectedVersion'"
}
if ($description.target -ne 'esp32s3') {
    throw "Unexpected target: $($description.target)"
}

$compiledCommit = Read-GeneratedString $generatedHeader 'LUOYE_CFG_GIT_COMMIT'
$compiledDirty = Read-GeneratedInt $generatedHeader 'LUOYE_CFG_GIT_DIRTY'
$compiledProductNameZh = Read-GeneratedString $generatedHeader 'LUOYE_CFG_PRODUCT_NAME_ZH'
$compiledHardware = Read-GeneratedString $generatedHeader 'LUOYE_CFG_HARDWARE_REV'
$compiledFlavor = Read-GeneratedString $generatedHeader 'LUOYE_CFG_BUILD_FLAVOR'
$compiledApi = Read-GeneratedString $generatedHeader 'LUOYE_CFG_API_CONTRACT'
$compiledServerRelease = Read-GeneratedString $generatedHeader 'LUOYE_CFG_SERVER_RELEASE'
$compiledMinimumClient = Read-GeneratedString $generatedHeader 'LUOYE_CFG_MIN_CLIENT_VERSION'
$compiledServerBaseUrl = Read-GeneratedString $generatedNetHeader 'LUOYE_CFG_SERVER_BASE_URL'
$compiledAllowHttp = Read-GeneratedInt $generatedNetHeader 'LUOYE_CFG_ALLOW_INSECURE_HTTP'
if (-not $commit.StartsWith($compiledCommit)) {
    throw "Build commit '$compiledCommit' does not match HEAD '$commit'"
}
if ($compiledDirty -ne 0) {
    throw 'The binary was compiled from a dirty or untracked source tree. Rebuild after committing the exact source.'
}
if ($compiledHardware -ne $HardwareRev) {
    throw "Build hardware '$compiledHardware' != manifest hardware '$HardwareRev'"
}
if ($compiledFlavor -ne $Profile) {
    throw "Build flavor '$compiledFlavor' != requested profile '$Profile'"
}
if ($compiledApi -ne $ApiContract) {
    throw "Build API '$compiledApi' != manifest API '$ApiContract'"
}
if ($compiledServerRelease -ne $ServerRelease) {
    throw "Build server release '$compiledServerRelease' != manifest '$ServerRelease'"
}
if ($compiledMinimumClient -ne $MinimumClientVersion) {
    throw "Build minimum client '$compiledMinimumClient' != manifest '$MinimumClientVersion'"
}
if ($compiledServerBaseUrl -ne $ServerBaseUrl) {
    throw "Build server '$compiledServerBaseUrl' != manifest server '$ServerBaseUrl'"
}
$expectedAllowHttp = if ($AllowInsecureHttp) { 1 } else { 0 }
if ($compiledAllowHttp -ne $expectedAllowHttp) {
    throw "Build insecure HTTP '$compiledAllowHttp' != requested '$expectedAllowHttp'"
}
if ($compiledAllowHttp -ne 0 -and $Profile -ne 'dev' -and $Profile -ne 'engineering') {
    throw 'Only dev/engineering packages may contain insecure HTTP support.'
}

# Do not trust a stale build directory that happened to carry matching version
# strings. Both patched drivers must resolve to this committed project tree.
$expectedLocalComponents = @(
    [IO.Path]::GetFullPath((Join-Path $project 'components\esp_driver_spi')),
    [IO.Path]::GetFullPath((Join-Path $project 'components\esp_driver_sdspi'))
)
$actualComponentPaths = @($description.build_component_paths |
    Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
    ForEach-Object {
        [IO.Path]::GetFullPath(([string]$_).Replace('/', '\')).TrimEnd('\')
    })
foreach ($expectedComponent in $expectedLocalComponents) {
    $matches = @($actualComponentPaths | Where-Object {
        $_.Equals($expectedComponent.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
    })
    if ($matches.Count -ne 1) {
        throw "Build does not contain exactly one project-local component: $expectedComponent"
    }
}

$criticalSources = [ordered]@{
    esp_driver_spi_master = (Join-Path $project 'components\esp_driver_spi\src\gpspi\spi_master.c')
    esp_driver_sdspi_host = (Join-Path $project 'components\esp_driver_sdspi\src\sdspi_host.c')
    esp_driver_sdspi_transaction = (Join-Path $project 'components\esp_driver_sdspi\src\sdspi_transaction.c')
    storage_runtime = (Join-Path $project 'components\storage_sd\storage_sd.c')
    upload_store = (Join-Path $project 'components\storage_sd\upload_store.c')
}
$criticalSourceHashes = [ordered]@{}
foreach ($sourceName in $criticalSources.Keys) {
    $sourcePath = $criticalSources[$sourceName]
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Critical source missing: $sourcePath"
    }
    $criticalSourceHashes[$sourceName] = Get-Sha256 $sourcePath
}

$flasher = Get-Content -LiteralPath $flasherPath -Raw -Encoding utf8 | ConvertFrom-Json
$expectedOffsets = @('0x0', '0x8000', '0x10000', '0x610000')
$actualOffsets = @($flasher.flash_files.PSObject.Properties.Name)
if (@(Compare-Object ($expectedOffsets | Sort-Object) ($actualOffsets | Sort-Object)).Count -ne 0) {
    throw "Unexpected flash layout: $($actualOffsets -join ', ')"
}

$buildPrefix = $build.TrimEnd('\') + '\'
$flashRows = foreach ($property in $flasher.flash_files.PSObject.Properties) {
    $relative = [string]$property.Value
    $source = [IO.Path]::GetFullPath((Join-Path $build ($relative -replace '/', '\')))
    if (-not $source.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Flash path escapes build root: $relative"
    }
    $item = Get-Item -LiteralPath $source
    [pscustomobject][ordered]@{
        offset = $property.Name
        offset_int = [Convert]::ToInt64($property.Name.Substring(2), 16)
        path = $relative
        source = $source
        bytes = $item.Length
        sha256 = Get-Sha256 $source
        encrypted = $false
    }
}
$flashRows = @($flashRows | Sort-Object offset_int)

for ($i = 0; $i -lt $flashRows.Count; $i++) {
    $end = $flashRows[$i].offset_int + $flashRows[$i].bytes
    if ($end -gt 16MB) { throw "Image exceeds 16MB flash: $($flashRows[$i].path)" }
    if ($i + 1 -lt $flashRows.Count -and $end -gt $flashRows[$i + 1].offset_int) {
        throw "Images overlap: $($flashRows[$i].path)"
    }
}

New-Item -ItemType Directory -Force -Path $output | Out-Null
$flashZip = Join-Path $output "$ReleaseId-flash.zip"
$symbolsZip = Join-Path $output "$ReleaseId-symbols.zip"
foreach ($target in @($flashZip, $symbolsZip, "$flashZip.sha256", "$symbolsZip.sha256")) {
    if (Test-Path -LiteralPath $target) { throw "Refusing to overwrite release artifact: $target" }
}

$token = [guid]::NewGuid().ToString('N')
$stageRoot = Join-Path $output ".staging-$token"
$flashStage = Join-Path $stageRoot 'flash'
$symbolsStage = Join-Path $stageRoot 'symbols'
New-Item -ItemType Directory -Force -Path $flashStage, $symbolsStage | Out-Null
$completed = $false

try {
    foreach ($row in $flashRows) {
        $destination = Join-Path $flashStage ($row.path -replace '/', '\')
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $row.source -Destination $destination
    }
    Copy-Item -LiteralPath (Join-Path $build 'flash_args') -Destination (Join-Path $flashStage 'flash_args')
    Copy-Item -LiteralPath $flasherPath -Destination (Join-Path $flashStage 'flasher_args.json')

    # Use the exact framework tree recorded by CMake. An unrelated IDF_PATH in
    # the packaging shell must never rewrite the build provenance.
    $idfRoot = [IO.Path]::GetFullPath(([string]$description.idf_path).Replace('/', '\'))
    $idfVersionFile = Join-Path $idfRoot 'tools\cmake\version.cmake'
    if (-not (Test-Path -LiteralPath $idfVersionFile -PathType Leaf)) {
        throw "ESP-IDF version metadata not found: $idfVersionFile"
    }
    $idfVersionText = Get-Content -LiteralPath $idfVersionFile -Raw -Encoding utf8
    $idfParts = foreach ($part in @('MAJOR', 'MINOR', 'PATCH')) {
        $match = [regex]::Match($idfVersionText, "(?m)^set\(IDF_VERSION_$part\s+([0-9]+)\)")
        if (-not $match.Success) { throw "Missing IDF_VERSION_$part in $idfVersionFile" }
        $match.Groups[1].Value
    }
    $idfVersion = "ESP-IDF v$($idfParts -join '.')"
    $idfCommit = (& git -C $idfRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve ESP-IDF commit: $idfRoot" }
    $idfDirtyRows = @(& git -C $idfRoot status --porcelain)
    $idfDirty = $idfDirtyRows.Count -gt 0
    $idfDescribe = (& git -C $idfRoot describe --always --tags $(if ($idfDirty) { '--dirty' })).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot describe ESP-IDF source: $idfRoot" }
    $manifestFlashRows = @($flashRows | ForEach-Object {
        [ordered]@{
            offset = $_.offset
            path = $_.path
            bytes = $_.bytes
            sha256 = $_.sha256
            encrypted = $_.encrypted
        }
    })
    $manifest = [ordered]@{
        schema = 1
        release_id = $ReleaseId
        channel = 'engineering'
        eligibility = 'engineering-only'
        created_utc = [DateTime]::UtcNow.ToString('o')
        product = [ordered]@{
            internal_name = 'Luoye'
            internal_name_zh = $compiledProductNameZh
            firmware_version = $ExpectedVersion
            hardware_revision = $HardwareRev
            build_profile = $Profile
        }
        source = [ordered]@{
            git_commit = $commit
            git_dirty = $dirty
            critical_source_sha256 = $criticalSourceHashes
        }
        compatibility = [ordered]@{
            api_contract = $ApiContract
            server_release = $ServerRelease
            minimum_client_version = $MinimumClientVersion
            server_base_url = $ServerBaseUrl
            insecure_http = [bool]$AllowInsecureHttp
        }
        toolchain = [ordered]@{
            target = $description.target
            idf = $idfVersion
            idf_git_commit = $idfCommit
            idf_git_describe = $idfDescribe
            idf_git_dirty = $idfDirty
            compiler = $description.c_compiler
        }
        runtime_contract = [ordered]@{
            range_block_bytes = 10485760
            sd_spi_frequency_hz = 20000000
            upload_mode = 'single_task_serial'
            sdspi_dma = 'static_internal_exact_wire'
        }
        flash = [ordered]@{
            mode = $flasher.flash_settings.flash_mode
            frequency = $flasher.flash_settings.flash_freq
            size = $flasher.flash_settings.flash_size
            files = $manifestFlashRows
        }
        security = [ordered]@{
            secure_boot = $false
            flash_encryption = $false
            nvs_encryption = $false
            ota_ab = $false
        }
        checks = [ordered]@{
            version_match = $true
            source_match = $true
            layout_valid = $true
            git_clean = -not $dirty
        }
    }
    $manifest | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $flashStage 'manifest.json') -Encoding utf8

    $flashingText = @"
# Luoye firmware flashing

Release: $ReleaseId

This package does not erase flash and does not overwrite NVS.

~~~powershell
python -m esptool --chip esp32s3 --port COMx --baud 460800 --before default_reset --after hard_reset write_flash --flash_mode $($flasher.flash_settings.flash_mode) --flash_freq $($flasher.flash_settings.flash_freq) --flash_size $($flasher.flash_settings.flash_size) 0x0 bootloader/bootloader.bin 0x8000 partition_table/partition-table.bin 0x10000 recorder_card.bin 0x610000 assets.bin
~~~
"@
    $flashingText | Set-Content -LiteralPath (Join-Path $flashStage 'FLASHING.md') -Encoding utf8
    $flashBatch = @"
@echo off
setlocal
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=COM22"
echo Flashing Luoye on %PORT% ...
python -m esptool --chip esp32s3 --port %PORT% --baud 460800 --before default_reset --after hard_reset write_flash --flash_mode $($flasher.flash_settings.flash_mode) --flash_freq $($flasher.flash_settings.flash_freq) --flash_size $($flasher.flash_settings.flash_size) 0x0 bootloader\bootloader.bin 0x8000 partition_table\partition-table.bin 0x10000 recorder_card.bin 0x610000 assets.bin
if errorlevel 1 (
  echo.
  echo Flash failed. Open ESP-IDF 5.5 CMD and run this file again.
  pause
  exit /b 1
)
echo.
echo Flash complete. The board will reset automatically.
pause
"@
    $flashBatch | Set-Content -LiteralPath (Join-Path $flashStage 'FLASH_COM22.bat') -Encoding ascii
    foreach ($extra in @(
        @{ Source = (Join-Path $project 'tools\export_power_csv.py'); Name = 'export_power_csv.py' },
        @{ Source = (Join-Path $project 'tools\EXPORT_POWER_CSV.bat'); Name = 'EXPORT_POWER_CSV.bat' },
        @{ Source = (Join-Path $project 'docs\POWER_SERIAL_EXPORT.md'); Name = 'POWER_SERIAL_EXPORT.md' }
    )) {
        if (-not (Test-Path -LiteralPath $extra.Source -PathType Leaf)) {
            throw "Missing serial export tool: $($extra.Source)"
        }
        $extraDestination = Join-Path $flashStage $extra.Name
        if ($extra.Name.EndsWith('.bat', [StringComparison]::OrdinalIgnoreCase)) {
            # cmd.exe requires Windows CRLF line endings. Reading by line and
            # writing as ASCII guarantees a portable batch file in the ZIP.
            Get-Content -LiteralPath $extra.Source -Encoding utf8 |
                Set-Content -LiteralPath $extraDestination -Encoding ascii
        } else {
            Copy-Item -LiteralPath $extra.Source -Destination $extraDestination
        }
    }
    Write-Checksums $flashStage
    Test-Checksums $flashStage

    $symbolSources = @(
        (Join-Path $build 'recorder_card.elf'),
        (Join-Path $build 'recorder_card.map'),
        (Join-Path $build 'bootloader\bootloader.elf'),
        (Join-Path $build 'bootloader\bootloader.map'),
        [string]$description.config_file,
        (Join-Path $project 'sdkconfig.defaults'),
        (Join-Path $project 'partitions.csv')
    )
    foreach ($source in $symbolSources) {
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $symbolsStage (Split-Path $source -Leaf))
        }
    }
    Copy-Item -LiteralPath (Join-Path $flashStage 'manifest.json') -Destination (Join-Path $symbolsStage 'manifest.json')
    Set-Content -LiteralPath (Join-Path $symbolsStage 'SOURCE.txt') -Encoding utf8 -Value @(
        "release_id=$ReleaseId",
        "git_commit=$commit",
        "git_dirty=$dirty",
        "idf_git_commit=$idfCommit",
        "idf_git_describe=$idfDescribe",
        "idf_git_dirty=$idfDirty",
        "branch=$((& git -C $project branch --show-current).Trim())"
    )
    Write-Checksums $symbolsStage
    Test-Checksums $symbolsStage

    Compress-Archive -Path (Join-Path $flashStage '*') -DestinationPath $flashZip -CompressionLevel Optimal
    Compress-Archive -Path (Join-Path $symbolsStage '*') -DestinationPath $symbolsZip -CompressionLevel Optimal

    foreach ($zip in @($flashZip, $symbolsZip)) {
        $verify = Join-Path $stageRoot ("verify-" + [IO.Path]::GetFileNameWithoutExtension($zip))
        Expand-Archive -LiteralPath $zip -DestinationPath $verify
        Test-Checksums $verify
        $zipHash = Get-Sha256 $zip
        Set-Content -LiteralPath "$zip.sha256" -Encoding ascii -Value "$zipHash  $([IO.Path]::GetFileName($zip))"
    }
    $completed = $true
}
finally {
    $resolvedOutput = [IO.Path]::GetFullPath($output).TrimEnd('\') + '\'
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($resolvedOutput, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
    if (-not $completed) {
        foreach ($partial in @($flashZip, $symbolsZip, "$flashZip.sha256", "$symbolsZip.sha256")) {
            $resolvedPartial = [IO.Path]::GetFullPath($partial)
            if ($resolvedPartial.StartsWith($resolvedOutput, [StringComparison]::OrdinalIgnoreCase) -and
                (Test-Path -LiteralPath $resolvedPartial -PathType Leaf)) {
                Remove-Item -LiteralPath $resolvedPartial -Force
            }
        }
    }
}

[pscustomobject]@{
    ReleaseId = $ReleaseId
    FlashZip = $flashZip
    FlashZipSha256 = Get-Sha256 $flashZip
    SymbolsZip = $symbolsZip
    SymbolsZipSha256 = Get-Sha256 $symbolsZip
}
