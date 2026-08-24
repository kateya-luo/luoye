[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Read-Source([string]$RelativePath) {
    Get-Content -LiteralPath (Join-Path $project $RelativePath) -Raw -Encoding utf8
}

$main = Read-Source 'main\app_main.c'
$net = Read-Source 'components\net_uploader\net_uploader.c'
$netHeader = Read-Source 'components\net_uploader\include\net_uploader.h'
$store = Read-Source 'components\storage_sd\upload_store.c'
$storeHeader = Read-Source 'components\storage_sd\include\upload_store.h'
$epd = Read-Source 'components\epd_ssd1681\epd_ssd1681.c'
$epdHeader = Read-Source 'components\epd_ssd1681\include\epd_ssd1681.h'
$stateHeader = Read-Source 'main\app_state.h'

foreach ($required in @(
    'IDLE_SLEEP_AFTER_MS 60000',
    'esp_light_sleep_start',
    'esp_sleep_enable_timer_wakeup',
    'rtc_restore_system',
    'APP_RENDER_CLOCK_PARTIAL',
    'PIN_KEY_REC',
    'PIN_KEY_MARK',
    'PIN_KEY_BACK',
    'PIN_RTC_INT',
    'net_idle_suspend',
    'net_idle_resume'
)) {
    if (($main + $net + $netHeader) -notmatch [regex]::Escape($required)) {
        throw "Idle/light-sleep contract is missing: $required"
    }
}

foreach ($required in @(
    'delete_all_closed',
    'sd_storage_delete_all_local',
    'status=deferred reason=session_open'
)) {
    if (($net + $store + $storeHeader) -notmatch [regex]::Escape($required)) {
        throw "Bulk-delete contract is missing: $required"
    }
}

foreach ($required in @('epd_frame_full', 'epd_frame_fast',
                         'epd_frame_partial_window', 'wait_busy',
                         's_panel[EPD_FB_BYTES]')) {
    if (($epd + $epdHeader) -notmatch [regex]::Escape($required)) {
        throw "Monochrome SSD1681 path is missing: $required"
    }
}

foreach ($removed in @('gray4', 'EPD_GRAY', '2-bpp',
                        'panel_aux', 'partial_old')) {
    if (($epd + $epdHeader + $stateHeader) -match [regex]::Escape($removed)) {
        throw "Removed grayscale/partial path is still compiled: $removed"
    }
}

Write-Output 'idle/light-sleep, bulk-delete and monochrome EPD static checks passed'
