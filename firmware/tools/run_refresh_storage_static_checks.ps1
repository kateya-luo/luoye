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

foreach ($required in @(
    'APP_RENDER_CLOCK_PARTIAL',
    'APP_RENDER_STATUS_PARTIAL',
    'HOME_CLOCK_BATTERY_X',
    'HOME_CLOCK_BATTERY_Y',
    'HOME_CLOCK_BATTERY_WIDTH',
    'HOME_CLOCK_BATTERY_HEIGHT',
    'epd_frame_partial_window(s_fb, HOME_CLOCK_BATTERY_X',
    'clock+battery-partial',
    '(utc.tm_min % 30) == 0',
    'local.tm_min == 0',
    '(local.tm_min % 10) == 0',
    'top_of_hour ? APP_RENDER_FULL',
    'ten_minute ? APP_RENDER_FAST',
    'idle_top_of_hour',
    'idle_ten_minute',
    'S.battery / 5U != previous_battery / 5U',
    'previous_charging == APP_CHG_CHARGING',
    '(five_second % 60) == 0',
    'if (status_page_visible()) CALL(render, APP_RENDER_STATUS_PARTIAL)',
    'event=page_transition_upgrade',
    'next_screen != s_panel_screen',
    'if (home_clock_visible()) CALL(render, APP_RENDER_CLOCK_PARTIAL)',
    'static bool home_clock_active(const app_state_t *state)',
    'displayed_home_minute',
    'displayed_home_minute < 0 || minute != displayed_home_minute',
    'if (panel_error == ESP_OK) displayed_home_minute = minute',
    'LY|UI_CLOCK|event=render_retry',
    'LY|UI_CLOCK|refresh=%s minute=%lld result=%s',
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

if ($ui -notmatch 'APP_RENDER_CLOCK_PARTIAL\)\s*\{[\s\S]{0,900}epd_frame_partial_window\(s_fb,\s*HOME_CLOCK_BATTERY_X') {
    throw 'Minute refresh must use the fixed home clock and battery window.'
}

$clockBranch = [regex]::Match(
    $ui,
    'else if \(kind == APP_RENDER_CLOCK_PARTIAL\) \{(?<body>[\s\S]*?)\r?\n  \} else if')
if (-not $clockBranch.Success) {
    throw 'Clock-partial render branch cannot be isolated.'
}
if ($clockBranch.Groups['body'].Value -match 'epd_frame_partial_auto\(s_fb\)') {
    throw 'Minute refresh must not collapse to an unreliable glyph-only auto-diff window.'
}

if ($ui -notmatch 'panel_error\s*==\s*ESP_OK\)\s*displayed_home_minute\s*=\s*minute') {
    throw 'A failed panel transaction must not acknowledge the displayed minute.'
}

if ($ui -match 'local\.tm_min\s*%\s*30') {
    throw 'The active home page must no longer FULL refresh at xx:30.'
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

if ($storage -match '\.format_if_mount_failed\s*=\s*true') {
    throw 'SD-card mount must not auto-format an unknown filesystem'
}

Write-Output 'fixed clock/battery refresh, retry and new-card provisioning checks passed'
