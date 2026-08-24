[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Read-Source([string]$RelativePath) {
    Get-Content -LiteralPath (Join-Path $project $RelativePath) -Raw -Encoding utf8
}

$main = Read-Source 'main\app_main.c'
$state = Read-Source 'main\app_state.c'
$stateHeader = Read-Source 'main\app_state.h'
$ui = Read-Source 'main\ui_render.c'
$storage = Read-Source 'components\storage_sd\storage_sd.c'
$storageHeader = Read-Source 'components\storage_sd\include\storage_sd.h'
$uploader = Read-Source 'components\net_uploader\net_uploader.c'
$sdkconfig = Read-Source 'sdkconfig.ui154'
$defaults = Read-Source 'sdkconfig.defaults'

foreach ($required in @(
    'APP_RENDER_CLOCK_PARTIAL',
    'APP_RENDER_STATUS_PARTIAL',
    'HOME_CLOCK_BATTERY_WIDTH',
    'epd_frame_partial_window(s_fb, HOME_CLOCK_BATTERY_X',
    'clock+battery-partial',
    '(utc.tm_min % 30) == 0',
    'RECORD_FAST_INTERVAL_SECONDS',
    'if (status_page_visible()) CALL(render, APP_RENDER_STATUS_PARTIAL)',
    'event=page_transition_upgrade',
    'next_screen != s_panel_screen',
    'if (home_clock_visible()) CALL(render, APP_RENDER_CLOCK_PARTIAL)',
    'static bool home_clock_active(const app_state_t *state)',
    'displayed_home_minute',
    'minute != displayed_home_minute',
    'LY|UI_CLOCK|refresh=%s minute=%lld result=%s',
    'LY|UI_CLOCK|event=render_retry',
    'bool network_online = net_is_online()',
    'bool account_bound = net_is_bound()',
    'network_online ? "已连接" : "离线"',
    'account_bound ? "已绑定" : "未绑定"',
    'LY|UI_STATUS|event=network_cache_mismatch'
)) {
    if (($main + $state + $stateHeader + $ui) -notmatch [regex]::Escape($required)) {
        throw "Partial-refresh contract is missing: $required"
    }
}

if ($ui -notmatch 'APP_RENDER_CLOCK_PARTIAL\)\s*\{[\s\S]{0,700}epd_frame_partial_window\(s_fb,\s*HOME_CLOCK_BATTERY_X') {
    throw 'Minute refresh must use the fixed combined home clock and battery window.'
}
if ($ui -match 'APP_RENDER_CLOCK_PARTIAL\)\s*\{[\s\S]{0,700}epd_frame_partial_auto\(s_fb\)') {
    throw 'Home minute refresh must not use an auto-diff glyph window after panel sleep.'
}

if ($ui -match 'state->cloud_online\s*\?\s*"已绑定"') {
    throw 'Device status must show persistent binding, not transient cloud readiness'
}

if ($state -match 'APP_RENDER_PAGE_PARTIAL' -or
    $ui -match 'epd_frame_partial_window\(s_fb,\s*0,\s*0,\s*200,\s*200\)') {
    throw 'Visual page transitions must use FAST, never a whole-panel partial waveform'
}

foreach ($required in @(
    '.format_if_mount_failed = false',
    'sd_mount_with_retry(false)',
    'storage_sd_format(void)',
    'esp_vfs_fat_sdcard_format_cfg',
    'APP_ERR_STORAGE_FORMAT_REQUIRED',
    'CARD_SCHEMA "luoye-storage/2"',
    'allocation_unit_size = 32 * 1024',
    'SD_MOUNT_ATTEMPTS',
    'prepare_card_layout',
    'luoye-card.json',
    'luoye-storage-write-test',
    'make_dir(SESSION_ROOT)',
    'make_dir(DIAG_ROOT)',
    'file_sync_close(file)'
)) {
    if ($storage -notmatch [regex]::Escape($required)) {
        throw "New/replacement SD-card contract is missing: $required"
    }
}

if ($storage -notmatch 'sd_mount_with_retry\(false\)[\s\S]{0,900}APP_ERR_STORAGE_FORMAT_REQUIRED') {
    throw 'Normal boot must request explicit formatting instead of auto-formatting.'
}

foreach ($required in @(
    'SD_DMA_PREPARE_GUARD_BYTES',
    'SD_DMA_LOW_WATER_BYTES (8U * 1024U)',
    'SD_DMA_LOW_WATER_TIMEOUT_MS',
    'wait_for_sd_dma_headroom',
    'event=backpressure',
    'read(fd, destination + *received, chunk)',
    'storage_sd_seek',
    'lseek(fd, (off_t)offset, SEEK_SET)',
    'storage_sd_size',
    'fstat(fd, &info)',
    'storage_sd_prepare_range',
    'event=range_prepare',
    'heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |',
    'CONFIG_FATFS_ALLOC_PREFER_EXTRAM is enabled'
)) {
    if (($storage + $storageHeader) -notmatch [regex]::Escape($required)) {
        throw "Offline-upload DMA guard is missing: $required"
    }
}
foreach ($source in @($storage, (Read-Source 'components\storage_sd\upload_store.c'),
                      (Read-Source 'components\net_uploader\net_uploader.c'),
                      (Read-Source 'components\agenda_todo\agenda_todo.c'))) {
    if ($source -match 'fseek\([^;]+;[\s\S]{0,500}storage_sd_read\(') {
        throw 'A descriptor read path still mixes fseek() with storage_sd_read().'
    }
}
if ($storage -match 's_dma_read_buffer') {
    throw 'Obsolete 16 KiB internal SD staging buffer must not consume DMA headroom.'
}
if ($sdkconfig -notmatch '(?m)^CONFIG_FATFS_ALLOC_PREFER_EXTRAM=y\r?$' -or
    $defaults -notmatch '(?m)^CONFIG_FATFS_ALLOC_PREFER_EXTRAM=y\r?$') {
    throw 'FatFS file/cache allocations must prefer PSRAM to preserve DMA RAM.'
}
foreach ($required in @(
    'CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y',
    'CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL=65536',
    'CONFIG_ESP_WIFI_STATIC_TX_BUFFER=y',
    'CONFIG_ESP_WIFI_TX_BUFFER_TYPE=0',
    'CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM=16',
    'CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM=16',
    'CONFIG_FATFS_SECTOR_512=y'
)) {
    if ($sdkconfig -notmatch [regex]::Escape($required)) {
        throw "Deterministic SD/Wi-Fi memory contract is missing: $required"
    }
}
if ($sdkconfig -match '(?m)^CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER=y\r?$') {
    throw 'Dynamic Wi-Fi TX buffers must not be able to exhaust SDSPI DMA RAM.'
}
if ($uploader -notmatch 'storage_sd_prepare_range\(file,\s*44U \+ offset\)[\s\S]{0,1800}esp_http_client_init') {
    throw 'The WAV sector cache must be prepared before HTTP/TCP allocation.'
}
if ($uploader -notmatch 'event=range_begin[^"]*free_internal=%u largest_dma=%u') {
    throw 'Range diagnostics must report internal/DMA headroom.'
}

Write-Output 'clock/status refresh, card provisioning and upload DMA checks passed'
