idf_component_register(
  SRCS "power_mgr.c" "power_soc.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES driver esp_hw_support
)
