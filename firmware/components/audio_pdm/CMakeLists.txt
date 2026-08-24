idf_component_register(
  SRCS "audio_pdm.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES driver esp_driver_i2s dualmic_frontend
)
