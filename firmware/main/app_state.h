#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * Luoye application state machine.
 * All functions must be called from the single app event-loop task.
 */
typedef enum {
  APP_MODE_OFF,
  APP_MODE_STANDBY,
  APP_MODE_STARTING,
  APP_MODE_RECORDING,
  APP_MODE_CLOSING,
  APP_MODE_ENDING,
  APP_MODE_STORAGE_ERROR,
  APP_MODE_PAIRING,
} app_mode_t;

typedef enum { APP_SCENE_MEETING, APP_SCENE_TRANSLATE } app_scene_t;
typedef enum {
  APP_OV_NONE,
  APP_OV_REMINDER,
  APP_OV_TODO_LISTEN,
  APP_OV_TODO_OK,
  APP_OV_TODO_CONFIRM,
  APP_OV_TODO_CREATED,
  APP_OV_TODO_FAILED,
  APP_OV_SNOOZED,
  APP_OV_LOCKED_HINT,
  APP_OV_POWER_CONFIRM,
  APP_OV_SYNC_CONFIRM,
  APP_OV_SYNC_PROGRESS,
  APP_OV_SYNC_DONE,
  APP_OV_SYNC_FAILED,
} app_overlay_t;
typedef enum { APP_CHG_NONE, APP_CHG_CHARGING, APP_CHG_FULL } app_charge_t;
typedef enum {
  APP_SYNC_IDLE = 0,
  APP_SYNC_RUNNING,
  APP_SYNC_DONE,
  APP_SYNC_FAILED,
} app_sync_state_t;
/* Ordered from the smallest update to the strongest waveform.  The UI queue
 * coalesces requests by keeping the numerically highest pending kind. */
typedef enum {
  APP_RENDER_CLOCK_PARTIAL = 0,
  APP_RENDER_STATUS_PARTIAL,
  APP_RENDER_PARTIAL,       /* active-recording body window */
  APP_RENDER_FAST,
  APP_RENDER_FULL,
} app_render_t;
typedef enum { APP_MARK_IMPORTANT, APP_MARK_FAV, APP_MARK_TODO } app_mark_kind_t;
typedef enum {
  APP_REMINDER_DISMISS = 0,
  APP_REMINDER_SNOOZE,
  APP_REMINDER_START_MEETING,
} app_reminder_action_t;
typedef enum {
  APP_TIMER_OVERLAY,
  APP_TIMER_ENDING,
  APP_TIMER_CLOSE_POLL,
  APP_TIMER_PAIRING,
  APP_TIMER_COUNT,
} app_timer_id_t;
typedef enum {
  APP_PAIR_IDLE = 0,
  APP_PAIR_AP_READY,
  APP_PAIR_WIFI_CONNECTING,
  APP_PAIR_WIFI_CONNECTED,
  APP_PAIR_CLAIM_PENDING,
  APP_PAIR_BOUND,
  APP_PAIR_ERROR,
} app_pair_state_t;

typedef enum {
  APP_ERR_NONE = 0,
  APP_ERR_NO_SD,
  APP_ERR_SD_FULL,
  APP_ERR_STORAGE_OPEN,
  APP_ERR_STORAGE_WRITE,
  APP_ERR_STORAGE_SYNC,
  APP_ERR_STORAGE_CLOSE,
  APP_ERR_STORAGE_TIMEOUT,
  APP_ERR_MIC,
  APP_ERR_LOW_BATTERY,
  APP_ERR_RECOVERY,
  APP_ERR_BUSY,
} app_error_t;

typedef enum {
  APP_CLOSE_USER = 0,
  APP_CLOSE_LOW_BATTERY,
  APP_CLOSE_STORAGE_ERROR,
} app_close_reason_t;

typedef enum {
  APP_EV_KEY_REC_SHORT,
  APP_EV_KEY_MARK_SHORT,
  APP_EV_KEY_BACK_SHORT,
  APP_EV_KEY_REC_LONG,
  APP_EV_KEY_REC_RELEASE,
  APP_EV_KEY_BACK_LONG,
  APP_EV_KEY_MARK_HOLD,
  APP_EV_KEY_MARK_RELEASE,
  APP_EV_RTC_ALARM,
  APP_EV_TIMER,
  APP_EV_NET_CHANGE,
  APP_EV_CLOUD_CHANGE,
  APP_EV_PAIRING_CHANGE,
  APP_EV_CHARGE_CHANGE,
  APP_EV_BATTERY,
  APP_EV_BATTERY_CRITICAL,
  APP_EV_SD_LOW,
  APP_EV_BACKLOG,
  APP_EV_AGENDA_CHANGE,
  APP_EV_TODO_RESULT,
  APP_EV_TIME_SYNC,
  APP_EV_SYNC_CHANGE,
  APP_EV_SESSION_CLOSE_DONE,
  APP_EV_SESSION_SETTLED,
  APP_EV_STORAGE_ERROR,
} app_event_t;

typedef struct {
  app_mode_t mode;
  app_scene_t scene;
  app_overlay_t overlay;
  app_error_t error;
  app_close_reason_t close_reason;
  uint8_t page;
  uint8_t agenda_page;
  bool paused, locked, online, cloud_online, sd_low;
  bool storage_settled;
  bool reminder_pending;
  int8_t todo_result_pending;
  app_pair_state_t pairing;
  app_sync_state_t sync;
  uint16_t backlog_s;
  uint8_t battery;
  app_charge_t charging;
  uint16_t marks, favs, todos;
  uint8_t close_poll_count;
  int64_t rec_start_ms, pause_started_ms, paused_total_ms, ended_elapsed_ms;
  int64_t todo_start_ms;
  char reminder[72];
} app_state_t;

typedef struct {
  int64_t (*now_ms)(void);
  void (*render)(app_render_t kind);
  app_error_t (*start_recording)(app_scene_t scene, const char *title);
  void (*stop_recording)(app_close_reason_t reason);
  void (*set_paused)(bool paused);
  void (*mark_point)(app_mark_kind_t kind, int64_t at_ms);
  void (*mark_flash)(void);
  bool (*todo_capture_start)(void);
  bool (*todo_capture_end)(bool saved);
  bool (*todo_action)(bool confirm);
  bool (*sync_request)(void);
  bool (*agenda_sync_request)(void);
  void (*reminder_action)(app_reminder_action_t action);
  void (*enter_pairing)(void);
  void (*exit_pairing)(void);
  void (*power_on)(void);
  void (*power_off)(void);
  void (*rtc_snooze_10min)(void);
  app_error_t (*recording_close_status)(void);
  void (*set_timer)(app_timer_id_t id, uint32_t ms);
  void (*cancel_timer)(app_timer_id_t id);
} app_hooks_t;

void app_state_init(const app_hooks_t *hooks);
void app_state_handle(app_event_t ev, int32_t arg);
const app_state_t *app_state_get(void);
int64_t app_state_elapsed_ms(void);
int64_t app_state_todo_elapsed_ms(void);
uint32_t app_hold_ms(char key);
void app_state_set_reminder(const char *title);
