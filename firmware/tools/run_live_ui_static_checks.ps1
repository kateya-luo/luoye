[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$protocol = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\live_protocol.c') -Raw
$store = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\upload_store.c') -Raw
$ui = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw
$state = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.c') -Raw

foreach ($required in @('/sessions/%s/state%s', 'after_revision', 'revision',
                         'include_partial=1', 'after_display_revision',
                         'after_caption_revision', 'after_speaker_revision',
                         'after_translation_revision', 'after_summary_revision',
                         'display_revision', 'partial_active', 'partial_text',
                         'received_samples', 'captions', 'translations',
                         'source_text', 'translated_text',
                         'sd_upload_current', 's_live_session_id',
                         'result_failed',
                         'status_text, "done"', 'status_text, "failed"',
                         'result.final ? "done"',
                         'LIVE_POLL_MS', 'LIVE_UPLOAD_CHECK_MS',
                         'luoye_live_caption_upsert',
                         'caption_generation', 'partial_generation')) {
    if (($net + $protocol) -notmatch [regex]::Escape($required)) {
        throw "Live AI client is missing: $required"
    }
}

foreach ($required in @('result_revision', 'display_revision',
                         'caption_revision', 'speaker_revision',
                         'translation_revision', 'summary_revision',
                         'live_chunk_bytes', 'result_pcm_bytes',
                         'safe_pcm_bytes', 'local_recording')) {
    if ($store -notmatch [regex]::Escape($required)) {
        throw "Live upload persistence is missing: $required"
    }
}

$header = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\include\live_protocol.h') -Raw
if ($header -notmatch 'LUOYE_LIVE_CAPTION_TEXT_BYTES\s+\(512 \+ 1\)') {
    throw 'Final caption payload must accept 512 UTF-8 bytes plus NUL.'
}

foreach ($required in @('LUOYE_LIVE_MEETING',
                         'net_live_snapshot', 'state->charging != APP_CHG_NONE',
                         'screen_live_timeline',
                         'layout_draw(&label, "时间戳纪要"',
                         '16, 18, 1, UI154_ALIGN_LEFT',
                         '24, 22, 1, UI154_ALIGN_LEFT',
                         '18, 21, 2, UI154_ALIGN_LEFT',
                         'ui_fb_rect(7, 80, 186, 1)',
                         'draw_timeline_leaf(3, 89)',
                         'draw_timeline_leaf(3, 132)',
                         'LY|UI_RECORD|refresh=',
                         'RECORD_FAST_INTERVAL_SECONDS',
                         'APP_RENDER_RECORD_LIVE_PARTIAL',
                         'LIVE_TICKER_HEIGHT',
                         'unified_caption_text',
                         'live_ticker_text', 'live_ticker_hash',
                         '24, 22, 1, UI154_ALIGN_LEFT',
                         'render_once_panel_snapshot',
                         'event=live_coalesced',
                         'event=live_catchup',
                         'refresh=ticker',
                         'Summary revisions are intentionally ignored here',
                         'APP_RENDER_RECORD_HEADER_PARTIAL',
                         'refresh=header',
                         'UI_TASK_STACK_BYTES 32768',
                         'epd_frame_fast',
                         'epd_frame_partial_window(s_fb, LIVE_TICKER_X, LIVE_TICKER_Y')) {
    if ($ui -notmatch [regex]::Escape($required)) {
        throw "EPD live interaction is missing: $required"
    }
}

if ($ui -match 'RECORD_LIVE_MIN_INTERVAL_MS') {
    throw 'Unified caption page still contains the obsolete post-refresh delay.'
}

if ($ui -notmatch '#define\s+RECORD_FAST_INTERVAL_SECONDS\s+60') {
    throw 'Recording summaries must be committed by the one-minute FAST cadence.'
}

if ($ui -match '18,\s*23,\s*6,\s*UI154_ALIGN_LEFT' -or
    $ui -match 'static void screen_caption' -or
    $ui -match 'APP_RENDER_RECORD_CAPTION_PARTIAL|timeline_content_hash|refresh=live-body' -or
    ($state + $ui) -match 'record_view|APP_RECORD_VIEW_') {
    throw 'Removed six-line/two-page recording UI is still reachable.'
}

if ($net -match 'ESP_LOG[A-Z]*\([^\r\n]*(meeting_text|source_text|translated_text)') {
    throw 'Transcript or translation content must not be written to engineering logs.'
}

Write-Output 'live AI/UI static checks passed'
