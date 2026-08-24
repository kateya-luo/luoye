idf_component_register(
  SRCS "net_uploader.c" "provisioning_form.c" "upload_protocol.c" "live_protocol.c"
  INCLUDE_DIRS "include"
  PRIV_INCLUDE_DIRS "../../main"
  REQUIRES esp_wifi esp_netif esp_event esp_http_client esp_http_server esp-tls
           esp_timer nvs_flash esp_hw_support storage_sd agenda_todo power_mgr json
           mbedtls
)

if(LUOYE_ALLOW_INSECURE_HTTP)
  set(LUOYE_ALLOW_INSECURE_HTTP_VALUE 1)
else()
  set(LUOYE_ALLOW_INSECURE_HTTP_VALUE 0)
endif()
configure_file(
  "${CMAKE_CURRENT_LIST_DIR}/luoye_net_config.h.in"
  "${CMAKE_CURRENT_BINARY_DIR}/generated/luoye_net_config.h"
  @ONLY
)
target_include_directories(${COMPONENT_LIB} PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}/generated")
