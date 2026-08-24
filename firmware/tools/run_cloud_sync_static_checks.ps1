[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$store = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\upload_store.c') -Raw
$storage = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\storage_sd.c') -Raw
$protocol = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\upload_protocol.c') -Raw
$defaults = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'sdkconfig.defaults') -Raw
$engineering = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'sdkconfig.ui154') -Raw

foreach ($required in @('/api/v2/build-info', '/api/v2/device/pair/start',
                         '/api/v2/device/pair/status', 'Idempotency-Key',
                         'X-Content-SHA256', 'X-Byte-Offset', 'X-Byte-Count',
                         'audio/L16;rate=16000;channels=1',
                         '/sessions/%s/audio/%lu', '/sessions/%s/marks/%s',
                         '/sessions/%s/end', 'binding_generation',
                         'AUDIO_CHUNKS_MISSING',
                         'state->valuestring, "processing"',
                         'state->valuestring, "done"',
                         'state->valuestring, "failed"',
                         'luoye_upload_read_mark_line',
                         'RANGE_BLOCK_BYTES   SD_UPLOAD_RANGE_BLOCK_BYTES',
                         'RANGE_STREAM_BYTES  (16U * 1024U)',
                         '/sessions/%s/upload-plan',
                         '/sessions/%s/audio-range',
                         '/sessions/%s/complete',
                         '/sessions/%s/live-resume',
                         '/sessions/%s/defer',
                         'live_acknowledged_bytes',
                         'LY|NETSCHED|lane=',
                         'LY|UPLOAD_DIAG|id=%s event=range_begin',
                         'event=range_hash result=%s',
                         'event=range_http result=%s',
                         'sd_read_ms=%llu', 'sha_ms=%llu',
                         'write_ms=%llu', 'effective_Bps=%llu',
                         'HTTP_TX_BUFFER_BYTES (8U * 1024U)',
                         'stream_range_serial',
                         'mode=serial',
                         'source=%s',
                         'bulk_wifi_ps_update((s_manual_sync || s_live_session_id[0]) && s_online)',
                         'esp_wifi_set_ps(WIFI_PS_NONE)',
                         'esp_wifi_set_ps(s_bulk_saved_wifi_ps)',
                         'LY|UPLOAD_WIFI_PS|state=performance',
                         'LY|UPLOAD_WIFI_PS|state=restored',
                         'LIVE_CHECKPOINT_CHUNKS 8U',
                         'LIVE_CHECKPOINT_MS    8000',
                         'LY|LIVE_UPLOAD_DIAG|id=%s',
                         'sd_upload_reader_read',
                         '.keep_alive_enable = true',
                         'sd_storage_delete_all_local')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "Cloud uploader is missing required contract element: $required"
    }
}

foreach ($required in @('CONFIG_LWIP_TCP_SND_BUF_DEFAULT=32768',
                         'CONFIG_LWIP_TCP_WND_DEFAULT=32768',
                         'CONFIG_LWIP_TCP_RECVMBOX_SIZE=24',
                         'CONFIG_LWIP_TCP_SACK_OUT=y')) {
    if ($defaults -notmatch [regex]::Escape($required) -or
        $engineering -notmatch [regex]::Escape($required)) {
        throw "Bulk-upload TCP profile is missing: $required"
    }
}
if ($net -match 'sd_upload_read_marks\(') {
    throw 'marks.jsonl must be streamed by line; whole-file 16 KiB reads can deadlock finalization.'
}
if ($net -match 'cloud_request\(HTTP_METHOD_PUT, url, "application/octet-stream"') {
    throw 'Session audio chunks must use the frozen audio/L16 media type.'
}

foreach ($required in @('remote_session_created', 'next_seq', 'acked_pcm_bytes',
                         'marks_acked', 'final_acked', 'retry_count',
                         'upload_mode', 'gap_start_bytes',
                         'live_resume_required', 'deferred_gaps', 'defer_acked')) {
    if ($store -notmatch [regex]::Escape($required)) {
        throw "Persistent upload state is missing: $required"
    }
}
foreach ($required in @('sd_upload_refresh_current', 'sd_upload_reader_close',
                         'reader->file_offset', 'storage_sd_seek(reader->file')) {
    if ($store -notmatch [regex]::Escape($required)) {
        throw "Sequential live reader/checkpoint support is missing: $required"
    }
}
foreach ($required in @('sd_upload_range_sha256', 'SD_UPLOAD_RANGE_HASH_FILE',
                         'SD_UPLOAD_RANGE_BLOCK_BYTES')) {
    if (($store + $net) -notmatch [regex]::Escape($required)) {
        throw "Precomputed range SHA support is missing: $required"
    }
}
foreach ($required in @('SD_SPI_FREQUENCY_KHZ SDMMC_FREQ_DEFAULT',
                         'sd_speed_probe',
                         'range_sha_disable', 'upload_fallback=scan',
                         'SD_DMA_READ_BYTES (16U * 1024U)',
                         'SD_DMA_LOW_WATER_BYTES (8U * 1024U)',
                         'wait_for_sd_dma_headroom',
                         'LY|STORAGE_DMA|event=backpressure',
                         'LY|STORAGE_DMA|event=policy mode=direct')) {
    if ($storage -notmatch [regex]::Escape($required)) {
        throw "Safe SD/SHA performance fallback is missing: $required"
    }
}
foreach ($required in @('xTaskCreateWithCaps(upload_task',
                         'MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT',
                         'LY|UPLOAD|event=task_ready stack=psram',
                         'LY|NET|event=task_create_failed task=uploader stack=psram')) {
    if (-not $net.Contains($required)) {
        throw "Upload task PSRAM-stack protection is missing: $required"
    }
}
foreach ($forbidden in @('range_reader_task', 'stream_range_pipeline',
                          'range_buffer_next', 'range_next',
                          'xQueueCreate(', 'xQueueReceive(', 'xQueueSend(')) {
    if ($net -match [regex]::Escape($forbidden)) {
        throw "Serial uploader must not retain the dual-task pipeline: $forbidden"
    }
}
if ($storage -match 'SDMMC_FREQ_HIGHSPEED' -or
    $storage -match 'LY\|STORAGE_PERF\|event=fallback') {
    throw 'SD SPI must stay fixed at 20 MHz without a 40 MHz first attempt.'
}
if ($storage -match 's_dma_read_buffer') {
    throw 'Obsolete internal SD staging must not consume DMA headroom.'
}
foreach ($required in @('fileno(file)',
                         'read(fd, destination + *received, chunk)')) {
    if (-not $storage.Contains($required)) {
        throw "Direct VFS SD read protection is missing: $required"
    }
}
foreach ($source in @($net, $store)) {
    if ($source -match 'fread\(') {
        throw 'Uploader SD reads must use storage_sd_read low-water backpressure.'
    }
    if ($source -notmatch 'storage_sd_read\(') {
        throw 'Uploader is missing low-water protected SD reads.'
    }
}
if ($net -match '2U\s*\*\s*1024U\s*\*\s*1024U') {
    throw 'API/2 must not contain a 2 MiB offline-upload fallback.'
}
if ($net -match 'xTaskCreate\(organizer_task') {
    throw 'Organizer must not own a second authenticated HTTP task.'
}

if ($net -notmatch 'sd_upload_next' -or $store -notmatch 'opendir\(SESSION_ROOT\)') {
    throw 'Uploader must scan all persisted session directories.'
}
if ($protocol -notmatch 'http_status == 401' -or
    $protocol -notmatch 'http_status == 409' -or
    $protocol -notmatch 'http_status == 429' -or
    $protocol -notmatch 'http_status >= 500') {
    throw 'HTTP retry/auth/conflict classes are incomplete.'
}
if ($net -match 'classification\s*!=\s*LUOYE_UPLOAD_HTTP_OK\s*&&\s*\r?\n\s*classification\s*!=\s*LUOYE_UPLOAD_HTTP_CONFLICT' -or
    $net -match 'status\s*!=\s*409') {
    throw 'Generic HTTP 409 must never be treated as a successful ACK.'
}
foreach ($required in @('net_request_manual_sync', 's_manual_sync',
                         'sd_storage_delete_local', 'phase=local_delete')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "Manual FIFO/delete-after-ACK policy is missing: $required"
    }
}
foreach ($removed in @('foreground', 'server_route', 'sd_upload_foreground')) {
    if (($net + $store) -match [regex]::Escape($removed)) {
        throw "Removed upload policy is still present: $removed"
    }
}
foreach ($forbidden in @('cancel_remote_before_local_delete',
                          '/sessions/%s/cancel',
                          'sd_storage_cleanup_synced',
                          'cleanup_synced',
                          'auto_cleanup')) {
    if ($net -match [regex]::Escape($forbidden)) {
        throw "SD deletion must not cancel or mutate a cloud meeting: $forbidden"
    }
}

Write-Output 'cloud-sync static checks passed'
