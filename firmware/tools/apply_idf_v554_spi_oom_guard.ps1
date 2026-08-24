[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$IdfPath
)

$ErrorActionPreference = 'Stop'
$spiMaster = Join-Path $IdfPath 'components\esp_driver_spi\src\gpspi\spi_master.c'
$sdspiHost = Join-Path $IdfPath 'components\esp_driver_sdspi\src\sdspi_host.c'

foreach ($path in @($spiMaster, $sdspiHost)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "ESP-IDF patch target not found: $path"
    }
}

$sdText = [IO.File]::ReadAllText($sdspiHost)
if ($sdText.Contains('LUOYE_V225_SDSPI_DMA_ALIGN')) {
    throw 'Unsafe V2.2.5 SDSPI wire-length patch is still present. Restore the official sdspi_host.c before building.'
}
$officialSdBlock = @(
    '        const size_t receive_extra_bytes = (rx_length > SDSPI_MAX_DATA_LEN) ? 4 : 2;',
    '        memset(rx_data, 0xff, will_receive + receive_extra_bytes);',
    '        spi_transaction_t t_data = {',
    '            .length = (will_receive + receive_extra_bytes) * 8,'
) -join "`n"
if (-not ($sdText.Replace("`r`n", "`n")).Contains($officialSdBlock)) {
    throw 'sdspi_host.c does not contain the verified ESP-IDF v5.5.4 transaction sequence.'
}

$spiText = [IO.File]::ReadAllText($spiMaster)
if (-not $spiText.Contains('LUOYE_V230_SPI_OOM_GUARD')) {
    $newline = if ($spiText.Contains("`r`n")) { "`r`n" } else { "`n" }
    $old = @(
        'clean_up:',
        '    uninstall_priv_desc(priv_desc);',
        '    return ret;'
    ) -join $newline
    $new = @(
        'clean_up:',
        '    /* LUOYE_V230_SPI_OOM_GUARD: setup_dma_priv_buffer only writes',
        '     * its output pointer after a successful allocation. Publish the',
        '     * still-valid local pointers before cleanup so RX OOM cannot make',
        '     * uninstall_priv_desc copy from a NULL private buffer. */',
        '    priv_desc->buffer_to_send = send_ptr;',
        '    priv_desc->buffer_to_rcv = rcv_ptr;',
        '    uninstall_priv_desc(priv_desc);',
        '    return ret;'
    ) -join $newline
    if (-not $spiText.Contains($old)) {
        throw 'spi_master.c does not match the verified ESP-IDF v5.5.4 cleanup context.'
    }
    [IO.File]::WriteAllText($spiMaster, $spiText.Replace($old, $new),
                            [Text.UTF8Encoding]::new($false))
}

Write-Output 'ESP-IDF v5.5.4 official SDSPI timing verified; SPI OOM guard is present.'
