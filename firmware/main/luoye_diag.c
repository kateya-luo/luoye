#include "luoye_diag.h"

#include <stdbool.h>
#include <inttypes.h>
#include <string.h>
#include "esp_log.h"

static const char *TAG = "luoye";
static luoye_diag_snapshot_t s_diag;

static const char *subsystem_name(luoye_subsystem_t subsystem) {
  static const char *NAMES[LUOYE_SUBSYS_COUNT] = {
    "nvs", "event_queue", "timers", "led", "ui",
    "keys", "power_i2c", "storage_sd", "audio_pdm", "network",
  };
  return subsystem < LUOYE_SUBSYS_COUNT ? NAMES[subsystem] : "unknown";
}

static const char *status_name(luoye_status_t status) {
  switch (status) {
    case LUOYE_STATUS_OK: return "OK";
    case LUOYE_STATUS_DEGRADED: return "DEGRADED";
    case LUOYE_STATUS_FAILED: return "FAILED";
    default: return "NOT_RUN";
  }
}

void luoye_diag_reset(void) {
  memset(&s_diag, 0, sizeof(s_diag));
}

void luoye_diag_set(luoye_subsystem_t subsystem, luoye_status_t status,
                    esp_err_t esp_error, luoye_error_code_t error_code) {
  if (subsystem >= LUOYE_SUBSYS_COUNT) return;
  s_diag.status[subsystem] = status;
  s_diag.esp_error[subsystem] = esp_error;
  s_diag.error_code[subsystem] = error_code;
  ESP_LOGI(TAG, "LY|INIT|subsystem=%s status=%s code=LYE-%03d esp=%s",
           subsystem_name(subsystem), status_name(status), (int)error_code,
           esp_err_to_name(esp_error));
}

void luoye_diag_note_event_drop(int32_t event_id, const char *reason) {
  uint32_t count = __atomic_add_fetch(&s_diag.event_drop_count, 1, __ATOMIC_RELAXED);
  // Log the first five drops and then powers of two to avoid a full queue
  // causing an additional serial-log storm.
  bool should_log = count <= 5 || (count & (count - 1)) == 0;
  if (should_log) {
    ESP_LOGE(TAG,
             "LY|EVENT_DROP|code=LYE-%03d event=%" PRId32 " count=%" PRIu32
             " reason=%s",
             LUOYE_ERR_EVENT_DROPPED, event_id, count,
             reason ? reason : "unknown");
  }
}

uint32_t luoye_diag_event_drop_count(void) {
  return __atomic_load_n(&s_diag.event_drop_count, __ATOMIC_RELAXED);
}

const luoye_diag_snapshot_t *luoye_diag_snapshot(void) { return &s_diag; }

void luoye_diag_log_snapshot(void) {
  uint32_t ok_mask = 0;
  uint32_t degraded_mask = 0;
  uint32_t failed_mask = 0;
  for (int i = 0; i < LUOYE_SUBSYS_COUNT; i++) {
    if (s_diag.status[i] == LUOYE_STATUS_OK) ok_mask |= 1U << i;
    else if (s_diag.status[i] == LUOYE_STATUS_DEGRADED) degraded_mask |= 1U << i;
    else if (s_diag.status[i] == LUOYE_STATUS_FAILED) failed_mask |= 1U << i;
  }
  ESP_LOGI(TAG,
           "LY|BOOT_READY|ok=0x%08" PRIx32 " degraded=0x%08" PRIx32
           " failed=0x%08" PRIx32 " event_drops=%" PRIu32,
           ok_mask, degraded_mask, failed_mask, luoye_diag_event_drop_count());
}
