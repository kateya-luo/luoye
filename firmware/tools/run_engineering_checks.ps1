[CmdletBinding()]
param(
    [ValidateSet('dev', 'rc', 'release', 'engineering')]
    [string]$Profile = 'engineering',
    [switch]$FullClean,
    [switch]$Package,
    [string]$ReleaseId = 'luoye-fw-v2.0.0-engineering-stable-sdspi-r1',
    [ValidatePattern('^https?://[A-Za-z0-9.-]+(:[0-9]+)?$')]
    [string]$ServerBaseUrl = 'https://meeting.example.invalid',
    [switch]$AllowInsecureHttp
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

if (@(& git -C $project status --porcelain).Count -gt 0) {
    throw 'Engineering checks require a clean Git working tree.'
}

& (Join-Path $PSScriptRoot 'run_state_test.bat')
if ($LASTEXITCODE -ne 0) { throw "State-machine test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_storage_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Storage-format test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_provisioning_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Provisioning-form test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_upload_protocol_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Upload-protocol test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_live_protocol_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Live-protocol test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_agenda_protocol_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Agenda-protocol test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_power_soc_test.bat')
if ($LASTEXITCODE -ne 0) { throw "Power-SOC calibration test failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_provisioning_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Provisioning static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_cloud_sync_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Cloud-sync static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_sdspi_exact_dma_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "SDSPI exact-wire DMA checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_live_ui_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Live AI/UI static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_agenda_todo_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Agenda/todo static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_manual_sync_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Manual-sync static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_bq100_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "BQ25186/power diagnostics static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_refresh_storage_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Refresh/storage provisioning static checks failed: $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'run_power_network_agenda_static_checks.ps1')
if ($LASTEXITCODE -ne 0) { throw "Power/network/agenda scheduling static checks failed: $LASTEXITCODE" }

$buildResult = & (Join-Path $PSScriptRoot 'build_profile.ps1') `
    -Profile $Profile `
    -FullClean:$FullClean `
    -ServerBaseUrl $ServerBaseUrl `
    -AllowInsecureHttp:$AllowInsecureHttp
$buildResult

if ($Package) {
    & (Join-Path $PSScriptRoot 'package_firmware.ps1') `
        -BuildDir $(if ($Profile -eq 'engineering') { 'build-v200' } else { "build-$Profile" }) `
        -ReleaseId $ReleaseId `
        -ExpectedVersion '2.0.0' `
        -ServerRelease '2.0.0' `
        -MinimumClientVersion '2.0.0' `
        -Profile $Profile `
        -ServerBaseUrl $ServerBaseUrl `
        -AllowInsecureHttp:$AllowInsecureHttp
}
