idf_component_register(
  SRCS "storage_sd.c" "storage_format.c" "upload_store.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES driver esp_driver_usb_serial_jtag fatfs sdmmc audio_pdm power_mgr json
           mbedtls nvs_flash esp_hw_support esp_rom esp_timer
)
