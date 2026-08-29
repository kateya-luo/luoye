# Project-local ESP-IDF 5.5.4 SPI driver

This component is a source-pinned copy of ESP-IDF 5.5.4 `esp_driver_spi` for
the ESP32-S3 firmware build. It contains two narrowly scoped fixes:

- `LUOYE_SPI_EXACT_LENGTH_DMA_BACKPORT`: backports Espressif's later
  `release/v5.5` DMA-alignment rule. An aligned internal DMA buffer may carry
  an exact non-word-sized transaction without allocating a private buffer.
- `LUOYE_V230_SPI_OOM_GUARD`: preserves valid local pointers on a private DMA
  allocation error before the common cleanup path runs.

The SDSPI caller still uses `SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL`, so an invalid
address fails closed. DMA descriptor capacity may be word-aligned, but the HAL
wire bit length remains the exact `spi_transaction_t.length` value.

The build is frozen to ESP-IDF 5.5.4 and ESP32-S3. When either changes, compare
this whole component with the new upstream implementation before carrying the
backport forward.
