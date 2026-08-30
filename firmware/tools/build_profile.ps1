[CmdletBinding()]
param(
    [ValidateSet('dev', 'rc', 'release', 'engineering')]
    [string]$Profile = 'engineering',
    [switch]$FullClean,
    [ValidatePattern('^https?://[A-Za-z0-9.-]+(:[0-9]+)?$')]
    [string]$ServerBaseUrl = 'https://meeting.example.invalid',
    [switch]$AllowInsecureHttp
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

if (-not $env:IDF_PATH) {
    throw 'ESP-IDF environment is not active. Open ESP-IDF PowerShell/CMD first.'
}

$idfPy = Join-Path $env:IDF_PATH 'tools\idf.py'
if (-not (Test-Path -LiteralPath $idfPy)) {
    throw "ESP-IDF driver not found: $idfPy"
}
$idfVersion = (& python $idfPy --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $idfVersion -notmatch 'v5\.5\.4') {
    throw "Luoye v2.0.0 requires ESP-IDF v5.5.4; detected: $idfVersion"
}

if ($ServerBaseUrl.StartsWith('http://')) {
    if (-not $AllowInsecureHttp) {
        throw 'HTTP requires -AllowInsecureHttp.'
    }
    if ($Profile -ne 'dev' -and $Profile -ne 'engineering') {
        throw 'HTTP is restricted to dev/engineering profiles.'
    }
}
$allowHttpValue = if ($AllowInsecureHttp) { 'ON' } else { 'OFF' }

if ($Profile -eq 'engineering') {
    $buildDir = Join-Path $project 'build-v200'
    $sdkconfigPath = Join-Path $project 'sdkconfig.ui154'
    $defaults = 'sdkconfig.defaults'
} else {
    $buildDir = Join-Path $project "build-$Profile"
    $sdkconfigPath = Join-Path $project "sdkconfig.$Profile"
    $defaults = "sdkconfig.defaults;sdkconfig.profile.$Profile"
}

Push-Location $project
try {
    if ($FullClean -and (Test-Path -LiteralPath $buildDir)) {
        & python $idfPy -B $buildDir fullclean
        if ($LASTEXITCODE -ne 0) { throw "idf.py fullclean failed: $LASTEXITCODE" }
    }

    & python $idfPy -B $buildDir `
        -D "SDKCONFIG=$sdkconfigPath" `
        -D "SDKCONFIG_DEFAULTS=$defaults" `
        -D "LUOYE_BUILD_FLAVOR=$Profile" `
        -D "LUOYE_SERVER_BASE_URL=$ServerBaseUrl" `
        -D "LUOYE_ALLOW_INSECURE_HTTP=$allowHttpValue" `
        reconfigure
    if ($LASTEXITCODE -ne 0) { throw "idf.py reconfigure failed: $LASTEXITCODE" }

    & python $idfPy -B $buildDir `
        -D "SDKCONFIG=$sdkconfigPath" `
        -D "SDKCONFIG_DEFAULTS=$defaults" `
        -D "LUOYE_BUILD_FLAVOR=$Profile" `
        -D "LUOYE_SERVER_BASE_URL=$ServerBaseUrl" `
        -D "LUOYE_ALLOW_INSECURE_HTTP=$allowHttpValue" `
        build
    if ($LASTEXITCODE -ne 0) { throw "idf.py build failed: $LASTEXITCODE" }

    $description = Get-Content -LiteralPath (Join-Path $buildDir 'project_description.json') -Raw |
        ConvertFrom-Json
    if ($description.project_version -ne '2.0.0') {
        throw "Embedded version mismatch: $($description.project_version)"
    }
    if ($description.target -ne 'esp32s3') {
        throw "Target mismatch: $($description.target)"
    }
    $expectedSdspi = [IO.Path]::GetFullPath(
        (Join-Path $project 'components\esp_driver_sdspi'))
    $actualSdspi = @($description.build_component_paths | Where-Object {
        $_ -and [IO.Path]::GetFullPath([string]$_) -eq $expectedSdspi
    })
    if ($actualSdspi.Count -ne 1) {
        throw 'Project-local esp_driver_sdspi override is not active.'
    }
    $expectedSpi = [IO.Path]::GetFullPath(
        (Join-Path $project 'components\esp_driver_spi'))
    $actualSpi = @($description.build_component_paths | Where-Object {
        $_ -and [IO.Path]::GetFullPath([string]$_) -eq $expectedSpi
    })
    if ($actualSpi.Count -ne 1) {
        throw 'Project-local esp_driver_spi override is not active.'
    }
    $expectedSdmmc = [IO.Path]::GetFullPath(
        (Join-Path $project 'components\sdmmc'))
    $actualSdmmc = @($description.build_component_paths | Where-Object {
        $_ -and [IO.Path]::GetFullPath([string]$_) -eq $expectedSdmmc
    })
    if ($actualSdmmc.Count -ne 1) {
        throw 'Project-local sdmmc override is not active.'
    }

    [pscustomobject]@{
        Product = 'Luoye'
        Version = $description.project_version
        Profile = $Profile
        ServerBaseUrl = $ServerBaseUrl
        InsecureHttp = [bool]$AllowInsecureHttp
        Target = $description.target
        BuildDirectory = $buildDir
    }
}
finally {
    Pop-Location
}
