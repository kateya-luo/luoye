[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$main = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_main.c') -Raw
$state = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.c') -Raw
$net = Get-Content -Encoding UTF8 -LiteralPath (
    Join-Path $project 'components\net_uploader\net_uploader.c') -Raw

foreach ($required in @(
    'net_idle_agenda_maintenance_start',
    'net_idle_agenda_maintenance_done',
    'net_idle_agenda_maintenance_stop',
    'net_request_agenda_sync',
    'IDLE_AGENDA_MAINTENANCE_MS',
    'APP_RENDER_FULL',
    'return false;')) {
    if ($main -notmatch [regex]::Escape($required)) {
        throw "Missing standby orchestration rule in app_main.c: $required"
    }
}

$rtcWake = [regex]::Match(
    $main,
    '(?s)if \(rtc_irq\) \{.*?app_post\(APP_EV_RTC_ALARM, 0\);.*?return false;.*?\}')
if (-not $rtcWake.Success -or $rtcWake.Value -match 'net_idle_resume') {
    throw 'RTC reminder wake must remain offline and must not become a full wake.'
}

$maintenanceLane = [regex]::Match(
    $net,
    '(?s)if \(s_idle_agenda_maintenance\) \{.*?\} else if \(!s_idle_suspended')
if (-not $maintenanceLane.Success -or
    $maintenanceLane.Value -notmatch 'agenda_sync_once\(false\)' -or
    $maintenanceLane.Value -match 'process_upload_item|process_todo|storage_sync_once') {
    throw 'Standby maintenance must expose the agenda-only network lane.'
}

foreach ($trigger in @(
    's_agenda_sync_requested = true',
    'idle_cloud_lane && s_agenda_sync_requested',
    'if (!s_idle_agenda_maintenance) s_agenda_sync_requested = true')) {
    if ($net -notmatch [regex]::Escape($trigger)) {
        throw "Missing forced-agenda trigger: $trigger"
    }
}

if ($state -notmatch 'CALL\(agenda_sync_request\)' -or
    $state -notmatch 'S\.page == 1') {
    throw 'Opening agenda must request sync, and agenda changes may repaint only that page.'
}

Write-Output 'power/network/agenda scheduling static checks passed'
