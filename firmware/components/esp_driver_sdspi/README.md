# Project-local SDSPI host driver

This is the ESP-IDF 5.5.4 SDSPI component pinned inside the Luoye firmware.
Its read protocol remains upstream-compatible: intermediate block reads use
up to 516 exact wire bytes and the final block uses no more than 514 bytes.

The component reserves one 516-byte, four-byte-aligned internal DMA buffer at
device initialization and uses it for payload, token, busy, CRC, and stop
transactions. Command objects use separately serialized internal DMA storage.
All SPI transactions are marked with
`SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL`. The paired project-local
`esp_driver_spi` component accepts exact non-word-sized lengths for aligned
internal ESP32-S3 DMA memory, so no upload-time private bounce allocation or
extra SD clock bytes are needed.

Do not round `spi_transaction_t.length` here. Descriptor capacity and wire
length are intentionally different concerns.

# Upstream overview

SD Host side related components are:
- `sdmmc`
- `esp_driver_sdmmc`
- `esp_driver_sdspi` (current component)

For relationship and dependency among these components, see [SD Host Side Related Component Architecture](../sdmmc/README.md).

`esp_driver_sdspi` components is a driver based on ESP GPSPI master driver to help you:
- do SD transactions (under SDSPI mode) via ESP GPSPI peripheral.
- tune ESP GPSPI hardware configurations, such as clock frequency, bus width, etc.
- ...

You can
- use this driver to implement `sdmmc` protocol interfaces
- directly use `esp_driver_sdspi` APIs

to communicate with SD slave devices under SDSPI mode.
