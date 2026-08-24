idf_component_register(
  SRCS "app_main.c" "app_state.c" "input_keys.c" "led_ctrl.c" "ui_render.c" "ui_font.c"
       "ui154_layout_generated.c"
       "luoye_build_info.c" "luoye_diag.c"
  INCLUDE_DIRS "."
  REQUIRES driver esp_timer nvs_flash esp_hw_support esp_app_format spi_flash esp_psram spiffs
           epd_ssd1681 audio_pdm storage_sd net_uploader power_mgr agenda_todo
)

configure_file(
  "${CMAKE_CURRENT_LIST_DIR}/luoye_build_config.h.in"
  "${CMAKE_CURRENT_BINARY_DIR}/generated/luoye_build_config.h"
  @ONLY
)
target_include_directories(${COMPONENT_LIB} PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")

# assets 分区镜像(含 font16.bin),idf.py flash 时一并烧录
spiffs_create_partition_image(assets ${CMAKE_CURRENT_LIST_DIR}/../assets FLASH_IN_PROJECT)
