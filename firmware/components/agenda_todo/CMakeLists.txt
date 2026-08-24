idf_component_register(
  SRCS "agenda_protocol.c" "agenda_todo.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES audio_pdm storage_sd power_mgr json fatfs esp_timer esp_hw_support mbedtls
)
