[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$store = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\upload_store.c') -Raw
$keys = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\input_keys.c') -Raw
$state = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.c') -Raw
$ui = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw

foreach ($required in @('PIN_KEY_REC', 'APP_EV_KEY_REC_LONG',
                         'PIN_KEY_MARK', 'APP_EV_KEY_MARK_SHORT')) {
    if ($keys -notmatch [regex]::Escape($required)) {
        throw "Middle-key sync input is missing: $required"
    }
}
foreach ($required in @('APP_OV_SYNC_CONFIRM', 'APP_OV_SYNC_PROGRESS',
                         'sync_request', "long_press('R')", "short_press('M')")) {
    if ($state -notmatch [regex]::Escape($required)) {
        throw "Manual-sync state flow is missing: $required"
    }
}
foreach ($required in @('screen_sync', 'APP_OV_SYNC_CONFIRM',
                         'APP_OV_SYNC_PROGRESS', 'APP_OV_SYNC_DONE',
                         'APP_OV_SYNC_FAILED')) {
    if ($ui -notmatch [regex]::Escape($required)) {
        throw "Manual-sync UI is missing: $required"
    }
}
foreach ($required in @('s_manual_sync', 's_live_session_id',
                         'sd_upload_current', 'sd_upload_find', 'sd_upload_next',
                         'APP_EV_SYNC_CHANGE', 'phase=local_delete',
                         'status=deferred reason=session_open',
                         's_manual_sync_request_revision',
                         'state=rearmed revision=',
                         'next=upload_plan',
                         'request_upload_plan(item, &plan)',
                         'history_scan == ESP_ERR_NOT_FOUND',
                         'state=failed reason=local_scan',
                         'local_ack=unchanged')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "Manual FIFO uploader is missing: $required"
    }
}
foreach ($forbidden in @('persist_manual_sync',
                          'nvs_set_u8(nvs, "manual_sync"',
                          'nvs_set_u32(nvs, "manual_gen"')) {
    if ($net -match [regex]::Escape($forbidden)) {
        throw "Minimal manual resume must not persist control state in NVS: $forbidden"
    }
}
foreach ($required in @('sd_storage_delete_local', 'sd_storage_delete_all_local',
                         'candidate.deletable = item.local_closed')) {
    if ($store -notmatch [regex]::Escape($required)) {
        throw "Explicit delete policy is missing: $required"
    }
}
foreach ($forbidden in @('cancel_remote_before_local_delete',
                          '/sessions/%s/cancel',
                          'sd_storage_cleanup_synced',
                          'cleanup_synced',
                          'auto_cleanup')) {
    if ($net -match [regex]::Escape($forbidden)) {
        throw "Local SD deletion is still coupled to cloud cancellation: $forbidden"
    }
}
foreach ($removed in @('foreground', 'sd_upload_foreground', 'net_set_idle',
                        'SD_UPLOAD_LIVE_CHUNK_BYTES')) {
    if (($net + $store) -match [regex]::Escape($removed)) {
        throw "Discarded upload/idle code is still present: $removed"
    }
}

Write-Output 'manual-sync/FIFO/delete static checks passed'
