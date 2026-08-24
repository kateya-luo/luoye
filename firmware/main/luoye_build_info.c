#include "luoye_build_info.h"

#include <inttypes.h>
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_psram.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "luoye_build_config.h"

static const char *TAG = "luoye";

const char *luoye_build_product_name(void) { return LUOYE_CFG_PRODUCT_NAME; }
const char *luoye_build_product_name_zh(void) { return LUOYE_CFG_PRODUCT_NAME_ZH; }
const char *luoye_build_version(void) { return esp_app_get_description()->version; }
const char *luoye_build_git_commit(void) { return LUOYE_CFG_GIT_COMMIT; }
const char *luoye_build_hardware_rev(void) { return LUOYE_CFG_HARDWARE_REV; }
const char *luoye_build_flavor(void) { return LUOYE_CFG_BUILD_FLAVOR; }
const char *luoye_build_api_contract(void) { return LUOYE_CFG_API_CONTRACT; }
const char *luoye_build_device_auth_profile(void) {
  return LUOYE_CFG_DEVICE_AUTH_PROFILE;
}
const char *luoye_build_server_release(void) { return LUOYE_CFG_SERVER_RELEASE; }
const char *luoye_build_min_client_version(void) { return LUOYE_CFG_MIN_CLIENT_VERSION; }
bool luoye_build_git_dirty(void) { return LUOYE_CFG_GIT_DIRTY != 0; }

static const char *reset_reason_name(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXTERNAL";
    case ESP_RST_SW: return "SOFTWARE";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "OTHER_WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SDIO: return "SDIO";
    case ESP_RST_USB: return "USB";
    case ESP_RST_JTAG: return "JTAG";
    case ESP_RST_EFUSE: return "EFUSE";
    case ESP_RST_PWR_GLITCH: return "POWER_GLITCH";
    case ESP_RST_CPU_LOCKUP: return "CPU_LOCKUP";
    default: return "UNKNOWN";
  }
}

static const char *wake_reason_name(esp_sleep_wakeup_cause_t cause) {
  switch (cause) {
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "UNDEFINED";
    case ESP_SLEEP_WAKEUP_ALL: return "ALL";
    case ESP_SLEEP_WAKEUP_EXT0: return "EXT0";
    case ESP_SLEEP_WAKEUP_EXT1: return "EXT1";
    case ESP_SLEEP_WAKEUP_TIMER: return "TIMER";
    case ESP_SLEEP_WAKEUP_TOUCHPAD: return "TOUCH";
    case ESP_SLEEP_WAKEUP_ULP: return "ULP";
    case ESP_SLEEP_WAKEUP_GPIO: return "GPIO";
    case ESP_SLEEP_WAKEUP_UART: return "UART";
    case ESP_SLEEP_WAKEUP_WIFI: return "WIFI";
    case ESP_SLEEP_WAKEUP_COCPU: return "COCPU";
    case ESP_SLEEP_WAKEUP_COCPU_TRAP_TRIG: return "COCPU_TRAP";
    case ESP_SLEEP_WAKEUP_BT: return "BT";
    default: return "OTHER";
  }
}

void luoye_build_log_boot(void) {
  const esp_app_desc_t *app = esp_app_get_description();
  char elf_sha[17] = {0};
  esp_app_get_elf_sha256(elf_sha, sizeof(elf_sha));

  esp_chip_info_t chip = {0};
  esp_chip_info(&chip);
  uint32_t flash_bytes = 0;
  esp_err_t flash_err = esp_flash_get_size(NULL, &flash_bytes);
  size_t psram_bytes = esp_psram_get_size();

  ESP_LOGI(TAG,
           "LY|BOOT|product=%s version=%s project=%s commit=%s dirty=%d "
           "hw=%s flavor=%s idf=%s build=%s_%s reset=%s wake=%s elf=%s",
           LUOYE_CFG_PRODUCT_NAME, app->version, app->project_name,
           LUOYE_CFG_GIT_COMMIT, LUOYE_CFG_GIT_DIRTY,
           LUOYE_CFG_HARDWARE_REV, LUOYE_CFG_BUILD_FLAVOR, app->idf_ver,
           app->date, app->time, reset_reason_name(esp_reset_reason()),
           wake_reason_name(esp_sleep_get_wakeup_cause()), elf_sha);

  ESP_LOGI(TAG,
           "LY|HW|model=%d rev=%u cores=%u flash=%" PRIu32
           " psram=%u heap=%" PRIu32 " flash_probe=%s",
           (int)chip.model, (unsigned)chip.revision, (unsigned)chip.cores,
           flash_bytes, (unsigned)psram_bytes, esp_get_free_heap_size(),
           esp_err_to_name(flash_err));

  ESP_LOGI(TAG,
           "LY|COMPAT|api=%s auth_profile=%s server=%s min_client=%s",
           LUOYE_CFG_API_CONTRACT, LUOYE_CFG_DEVICE_AUTH_PROFILE,
           LUOYE_CFG_SERVER_RELEASE, LUOYE_CFG_MIN_CLIENT_VERSION);
}
