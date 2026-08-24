[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$agenda = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\agenda_todo\agenda_todo.c') -Raw
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$power = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\power_mgr\power_mgr.c') -Raw
$audio = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\audio_pdm\audio_pdm.c') -Raw
$state = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.c') -Raw
$ui = (Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw) +
      (Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui154_layout_generated.c') -Raw)
$layout = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'docs\luoye_ui_layout_23pages_with_icons.json') -Raw
$unscheduled = -join ([char]0x672a, [char]0x5b9a, [char]0x65f6, [char]0x95f4)

foreach ($required in @('/api/v2/device/agenda%s&window_days=7',
                         '/api/v2/device/todos/%s/audio?binding_generation=',
                         '/todos/%s/result?after_revision=', '/todos/%s/actions',
                         'Idempotency-Key', 's_binding_generation',
                         'latest.binding_generation == s_binding_generation',
                         'todo:%s:action:%s:%lu', 'TODO_REVISION_MISMATCH',
                         '\"revision\":%lu', '"server_id"')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "Agenda/todo network contract is missing: $required"
    }
}
foreach ($required in @('agenda.json', 'todo.json', 'write_json_atomic',
                         'fsync', 'LUOYE_TODO_QUEUED', 'TODO_MAX_PCM',
                         'item.binding_generation != binding_generation')) {
    if ($agenda -notmatch [regex]::Escape($required)) {
        throw "Agenda/todo persistence is missing: $required"
    }
}
foreach ($required in @('rtc_set_alarm_utc', 'bcd(utc.tm_mday)',
                         'esp_light_sleep_start', 'PIN_RTC_INT')) {
    if ($power -notmatch [regex]::Escape($required)) {
        throw "RTC reminder path is missing: $required"
    }
}
if ($audio -notmatch 'if \(tap\) tap\(stereo, frames, tap_ctx\);\s*if \(muted\)') {
    throw 'Voice todo tap must remain active while the main recording is paused.'
}
foreach ($required in @('todo_result_pending', 'APP_OV_TODO_CONFIRM',
                         'APP_REMINDER_SNOOZE', 'S.agenda_page++',
                         "S.page = S.page == 0 ? 2 : 0")) {
    if ($state -notmatch [regex]::Escape($required)) {
        throw "State-machine agenda/todo behavior is missing: $required"
    }
}
foreach ($required in @('agenda_snapshot_get', '02_agenda', $unscheduled,
                         'agenda_collect_visible', 'state->agenda_page % page_count',
                         'custom_1786374727793',
                         'white_rect(4, 176, 125, 22)',
                         'layout_draw_field(page, "next_label", next_time)',
                         'layout_draw_field(page, "next_event", next_content)',
                         'APP_OV_TODO_CONFIRM', 'epd_frame_fast(s_fb)',
                         'event=page_transition_upgrade',
                         'epd_frame_full(s_fb)')) {
    if ($ui -notmatch [regex]::Escape($required)) {
        throw "Agenda/todo UI or FAST refresh policy is missing: $required"
    }
}
if ($ui -match 's->online\s*\?\s*"\u5df2\u540c\u6b65"') {
    throw 'WiFi reachability must never be presented as confirmed cloud sync.'
}
if ($layout -match [regex]::Escape('待办键 下一页')) {
    throw 'The retired agenda next-page hint must not remain in the layout.'
}
foreach ($required in @('剩余约%u秒', 'APP_RENDER_STATUS_PARTIAL')) {
    if (($ui + $state) -notmatch [regex]::Escape($required)) {
        throw "Sync progress partial-refresh behavior is missing: $required"
    }
}

Write-Output 'agenda/todo static checks passed'
