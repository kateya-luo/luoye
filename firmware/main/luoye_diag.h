// Machine-readable startup diagnostics and stable Luoye engineering error IDs.
#pragma once

#include <stdint.h>
#include "esp_err.h"

typedef enum {
  LUOYE_SUBSYS_NVS = 0,
  LUOYE_SUBSYS_EVENT_QUEUE,
  LUOYE_SUBSYS_TIMERS,
  LUOYE_SUBSYS_LED,
  LUOYE_SUBSYS_UI,
  LUOYE_SUBSYS_KEYS,
  LUOYE_SUBSYS_POWER,
  LUOYE_SUBSYS_STORAGE,
  LUOYE_SUBSYS_AUDIO,
  LUOYE_SUBSYS_NETWORK,
  LUOYE_SUBSYS_COUNT,
} luoye_subsystem_t;

typedef enum {
  LUOYE_STATUS_NOT_RUN = 0,
  LUOYE_STATUS_OK,
  LUOYE_STATUS_DEGRADED,
  LUOYE_STATUS_FAILED,
} luoye_status_t;

typedef enum {
  LUOYE_ERR_NONE = 0,
  LUOYE_ERR_NVS_INIT = 100,
  LUOYE_ERR_EVENT_QUEUE_CREATE = 110,
  LUOYE_ERR_EVENT_DROPPED = 111,
  LUOYE_ERR_TIMER_CREATE = 120,
  LUOYE_ERR_LED_INIT = 200,
  LUOYE_ERR_UI_INIT = 210,
  LUOYE_ERR_KEYS_INIT = 220,
  LUOYE_ERR_POWER_INIT = 300,
  LUOYE_ERR_STORAGE_INIT = 310,
  LUOYE_ERR_AUDIO_INIT = 320,
  LUOYE_ERR_NETWORK_INIT = 330,
} luoye_error_code_t;

typedef struct {
  luoye_status_t status[LUOYE_SUBSYS_COUNT];
  esp_err_t esp_error[LUOYE_SUBSYS_COUNT];
  luoye_error_code_t error_code[LUOYE_SUBSYS_COUNT];
  uint32_t event_drop_count;
} luoye_diag_snapshot_t;

void luoye_diag_reset(void);
void luoye_diag_set(luoye_subsystem_t subsystem, luoye_status_t status,
                    esp_err_t esp_error, luoye_error_code_t error_code);
void luoye_diag_note_event_drop(int32_t event_id, const char *reason);
uint32_t luoye_diag_event_drop_count(void);
const luoye_diag_snapshot_t *luoye_diag_snapshot(void);
void luoye_diag_log_snapshot(void);
