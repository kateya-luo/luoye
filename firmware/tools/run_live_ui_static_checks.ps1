[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$protocol = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\live_protocol.c') -Raw
$store = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\upload_store.c') -Raw
$ui = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw

foreach ($required in @('/sessions/%s/state%s', 'after_revision', 'revision',
                         'received_samples', 'captions', 'translations',
                         'source_text', 'translated_text',
                         'sd_upload_current', 's_live_session_id',
                         'result_failed',
                         'status_text, "done"', 'status_text, "failed"',
                         'result.final ? "done"',
                         'LIVE_POLL_MS', 'luoye_live_append_text')) {
    if (($net + $protocol) -notmatch [regex]::Escape($required)) {
        throw "Live AI client is missing: $required"
    }
}

foreach ($required in @('result_revision', 'result_pcm_bytes',
                         'safe_pcm_bytes', 'local_recording')) {
    if ($store -notmatch [regex]::Escape($required)) {
        throw "Live upload persistence is missing: $required"
    }
}

foreach ($required in @('LUOYE_LIVE_MEETING',
                         'net_live_snapshot', 'state->charging != APP_CHG_NONE',
                         'LY|UI_RECORD|refresh=',
                         'seconds / 5', 'five_second % 60', 'periodic_fast',
                         'epd_frame_fast',
                         'epd_frame_partial_window(s_fb, 4, 8, 192, 171)',
                         'layout_draw_field_latest')) {
    if ($ui -notmatch [regex]::Escape($required)) {
        throw "EPD live interaction is missing: $required"
    }
}

foreach ($removed in @('APP_RECORD_VIEW_TIMELINE', 'screen_chapter_summary',
                        '时间戳纪要', 'live_timeline_snapshot')) {
    if (($ui + $net) -match [regex]::Escape($removed)) {
        throw "V0.21 transcript-only firmware still contains rolling-minutes UI: $removed"
    }
}

foreach ($required in @('transcript_only_live_v1',
                         'canonical_offline_diarization_v2')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "V0.21 server capability gate is missing: $required"
    }
}

if ($net -match 'ESP_LOG[A-Z]*\([^\r\n]*(meeting_text|source_text|translated_text)') {
    throw 'Transcript or translation content must not be written to engineering logs.'
}

Write-Output 'live AI/UI static checks passed'
