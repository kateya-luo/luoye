idf_component_register(
  SRCS "epd_ssd1680.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES driver esp_timer
)
