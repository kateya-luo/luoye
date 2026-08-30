[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sdspiPath = Join-Path $project 'components\esp_driver_sdspi\src\sdspi_host.c'
$sdspiTransactionPath = Join-Path $project 'components\esp_driver_sdspi\src\sdspi_transaction.c'
$spiPath = Join-Path $project 'components\esp_driver_spi\src\gpspi\spi_master.c'
$storagePath = Join-Path $project 'components\storage_sd\storage_sd.c'
$uploaderPath = Join-Path $project 'components\net_uploader\net_uploader.c'

$sdspi = Get-Content -LiteralPath $sdspiPath -Raw -Encoding UTF8
$sdspiTransaction = Get-Content -LiteralPath $sdspiTransactionPath -Raw -Encoding UTF8
$spi = Get-Content -LiteralPath $spiPath -Raw -Encoding UTF8
$storage = Get-Content -LiteralPath $storagePath -Raw -Encoding UTF8
$uploader = Get-Content -LiteralPath $uploaderPath -Raw -Encoding UTF8

foreach ($required in @(
    'SDSPI_BLOCK_BUF_SIZE    (SDSPI_MAX_DATA_LEN + 4)',
    'heap_caps_aligned_alloc(SDSPI_DMA_ALIGNMENT',
    'MALLOC_CAP_INTERNAL |',
    'MALLOC_CAP_DMA',
    'assert(esp_ptr_in_dram(slot->block_buf))',
    'assert(esp_ptr_dma_capable(slot->block_buf))',
    '.flags = SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL',
    '.length = receive_bytes * 8',
    'const size_t expected_data_size = MIN(rx_length, SDSPI_MAX_DATA_LEN)',
    'if (extra_data_size > expected_data_size)',
    'pre_scan_data_size = receive_extra_bytes - sizeof(crc)')) {
    if (-not $sdspi.Contains($required)) {
        throw "Missing exact-wire SDSPI invariant: $required"
    }
}

$manualTransactions = [regex]::Matches(
    $sdspi, '\.flags\s*=\s*SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL').Count
if ($manualTransactions -ne 12) {
    throw "Every one of the 12 SDSPI SPI transactions must use the permanent DMA path; found $manualTransactions"
}
if ($sdspi -match 'SPI_TRANS_USE_RXDATA|SPI_TRANS_USE_TXDATA') {
    throw 'SDSPI must not retain inline/stack-backed DMA transactions.'
}
$preserveLookAhead = $sdspi.IndexOf('memcpy(data, extra_data_ptr, extra_data_size);')
$clearDmaBuffer = $sdspi.IndexOf('memset(rx_data, 0xff, SDSPI_BLOCK_BUF_SIZE);')
if ($preserveLookAhead -lt 0 -or $clearDmaBuffer -lt 0 -or
    $preserveLookAhead -gt $clearDmaBuffer) {
    throw 'Read look-ahead must be preserved before the permanent DMA buffer is cleared.'
}
foreach ($required in @('static DMA_ATTR sdspi_hw_cmd_t hw_cmd',
                         'assert(esp_ptr_in_dram(&hw_cmd))',
                         'assert(esp_ptr_dma_capable(&hw_cmd))')) {
    if (-not $sdspiTransaction.Contains($required)) {
        throw "SDSPI command still depends on a PSRAM-stack DMA bounce: $required"
    }
}

foreach ($forbidden in @('dma_receive_bytes',
                          '(receive_bytes + SDSPI_DMA_ALIGNMENT - 1)',
                          '.length = SDSPI_BLOCK_BUF_SIZE * 8')) {
    if ($sdspi.Contains($forbidden)) {
        throw "Forbidden SDSPI wire-padding pattern remains: $forbidden"
    }
}

foreach ($required in @('LUOYE_SPI_EXACT_LENGTH_DMA_BACKPORT',
                         'host->bus_attr->cache_align_int > 1',
                         'const uint32_t align_len',
                         'memcpy(temp, buffer, len)',
                         'LUOYE_V230_SPI_OOM_GUARD',
                         'hal_trans->tx_bitlen = trans->length',
                         'hal_trans->rx_bitlen = trans->rxlength')) {
    if (-not $spi.Contains($required)) {
        throw "Missing SPI-master DMA backport invariant: $required"
    }
}

for ($extraData = 0; $extraData -le 7; $extraData++) {
    $payloadRemaining = 512 - $extraData
    foreach ($lookAhead in @(4, 2)) {
        $wireBytes = $payloadRemaining + $lookAhead
        $descriptorCapacity = [int](($wireBytes + 3) -band (-bnot 3))
        if ($wireBytes -gt 516 -or $descriptorCapacity -gt 516) {
            throw "Buffer bound failed: extra=$extraData lookAhead=$lookAhead"
        }
        if ($lookAhead -eq 2 -and $extraData -eq 0 -and $wireBytes -ne 514) {
            throw 'Final full block must clock exactly 514 bytes.'
        }
    }
}

foreach ($required in @('event=probe_cleanup_skipped reason=storage_fault',
                         'if (!storage_sd_faulted()) unlink(temp)',
                         'action=block_runtime_io',
                         'setvbuf(s_sess.wav, NULL, _IONBF, 0)',
                         'static DMA_ATTR int16_t buffer[WRITE_SAMPLES]',
                         'free_internal=%lu free_dma=%lu largest_dma=%lu')) {
    if (-not $storage.Contains($required)) {
        throw "Missing post-fault zero-mutation guard: $required"
    }
}
$schedulerStart = $uploader.IndexOf('bulk_wifi_ps_update(s_manual_sync && s_online);')
$schedulerFaultGate = $uploader.IndexOf('if (storage_sd_faulted()) {', $schedulerStart)
$schedulerBacklog = $uploader.IndexOf('if (now_ms >= next_backlog_ms) {', $schedulerStart)
if ($schedulerStart -lt 0 -or $schedulerFaultGate -lt 0 -or
    $schedulerBacklog -lt 0 -or $schedulerFaultGate -gt $schedulerBacklog) {
    throw 'Storage FAULTED gate must precede backlog scanning.'
}

Write-Output 'SDSPI exact-wire DMA static checks passed'
