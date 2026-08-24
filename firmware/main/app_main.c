// app_main.c — 装配层:状态机(app_state)是唯一决策者,这里把它的副作用回调
// 接到 EPD/音频/SD/网络/电源各子系统,并提供单一事件队列(所有事件串行进状态机)。
//
// 任务全景:
//   keys(10ms 轮询) ─┐
//   power_poll(5s)  ─┼→ [app 事件队列] → app_task → app_state → hooks
//   uploader(3s)    ─┘                                   │
//   audio_cap(核1,prio18) → StreamBuffer → sd_writer(核0,prio15)
//   ui(渲染,prio6) ← ui_request_render        led(50ms 花样)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "nvs_flash.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "esp_log.h"
#include "esp_system.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"

#include "board_pins.h"
#include "app_state.h"
#include "input_keys.h"
#include "led_ctrl.h"
#include "ui_render.h"
#include "epd_ssd1681.h"
#include "audio_pdm.h"
#include "storage_sd.h"
#include "net_uploader.h"
#include "power_mgr.h"
#include "agenda_todo.h"
#include "luoye_build_info.h"
#include "luoye_diag.h"

static const char *TAG = "luoye";

// ---------- 事件队列 ----------
typedef struct { app_event_t ev; int32_t arg; } app_msg_t;
static QueueHandle_t s_q;
static bool s_ui_ready;
static bool s_audio_ready;
static bool s_todo_started_audio;

#define IDLE_SLEEP_AFTER_MS 60000
#define IDLE_WAKE_FALLBACK_US (60ULL * 1000000ULL)
#define IDLE_AGENDA_MAINTENANCE_MS 30000
#define IDLE_AGENDA_STOP_GRACE_MS 22000

static bool event_is_critical(app_event_t ev) {
  return ev == APP_EV_TIMER || ev == APP_EV_RTC_ALARM ||
         ev == APP_EV_TODO_RESULT ||
         ev == APP_EV_SESSION_CLOSE_DONE || ev == APP_EV_SESSION_SETTLED ||
         ev == APP_EV_STORAGE_ERROR;
}

static bool event_must_arrive(app_event_t ev) {
  return ev == APP_EV_SESSION_CLOSE_DONE || ev == APP_EV_SESSION_SETTLED ||
         ev == APP_EV_STORAGE_ERROR;
}

static void app_post(app_event_t ev, int32_t arg) {
  app_msg_t m = {ev, arg};
  if (!s_q) {
    luoye_diag_note_event_drop((int32_t)ev, "queue_not_ready");
    return;
  }
  TickType_t wait = event_must_arrive(ev)
                      ? portMAX_DELAY
                      : (event_is_critical(ev) ? pdMS_TO_TICKS(250) : 0);
  if (xQueueSend(s_q, &m, wait) != pdPASS) {
    luoye_diag_note_event_drop((int32_t)ev, "queue_full");
  }
}

static void log_main_stack(const char *stage) {
  UBaseType_t free_units = uxTaskGetStackHighWaterMark(NULL);
  ESP_LOGI(TAG, "LY|STACK|task=main stage=%s free=%u unit_bytes=%u",
           stage, (unsigned)free_units, (unsigned)sizeof(StackType_t));
}

// ---------- 状态机定时器(esp_timer 单次,回调只投递事件) ----------
static esp_timer_handle_t s_timers[APP_TIMER_COUNT];
static void timer_cb(void *arg) { app_post(APP_EV_TIMER, (int32_t)(intptr_t)arg); }
static void hook_set_timer(app_timer_id_t id, uint32_t ms) {
  esp_timer_stop(s_timers[id]);
  esp_timer_start_once(s_timers[id], (uint64_t)ms * 1000);
}
static void hook_cancel_timer(app_timer_id_t id) { esp_timer_stop(s_timers[id]); }

static bool hook_storage_format(void) {
  esp_err_t error = storage_sd_format();
  ESP_LOGI(TAG, "LY|STORAGE_FORMAT|event=request result=%s",
           esp_err_to_name(error));
  if (error != ESP_OK) return false;
  vTaskDelay(pdMS_TO_TICKS(200));
  esp_restart();
  return true;
}

// ---------- 录音会话 hooks ----------
// 只有四个会话文件均已同步、麦克风成功启动后才进入 RECORDING。
static app_error_t hook_start_recording(app_scene_t scene, const char *title) {
  if (!storage_sd_mounted()) {
    ESP_LOGE(TAG, "LY|REC|action=start result=failed subsystem=storage reason=no_sd");
    return APP_ERR_NO_SD;
  }
  if (!s_audio_ready) {
    ESP_LOGE(TAG, "LY|REC|action=start result=failed subsystem=audio reason=not_initialized");
    return APP_ERR_MIC;
  }
  char sid[48];
  esp_err_t id_err = sd_session_generate_id(sid, sizeof(sid));
  if (id_err != ESP_OK) {
    ESP_LOGE(TAG, "LY|REC|action=start result=failed subsystem=nvs esp=%s",
             esp_err_to_name(id_err));
    return APP_ERR_STORAGE_OPEN;
  }
  power_set_low_noise(true);                 // TPS63001 强制 PWM,压住录音底噪
  esp_err_t audio_err = audio_pdm_start();
  if (audio_err != ESP_OK) {
    ESP_LOGE(TAG, "LY|REC|action=start result=failed subsystem=audio esp=%s",
             esp_err_to_name(audio_err));
    power_set_low_noise(false);
    return APP_ERR_MIC;
  }
  app_error_t storage_result = sd_session_open(sid, scene, title);
  if (storage_result != APP_ERR_NONE) {
    ESP_LOGE(TAG, "LY|REC|action=start result=failed subsystem=storage code=%d",
             (int)storage_result);
    audio_pdm_stop();
    power_set_low_noise(false);
    return storage_result;
  }
  net_session_begin(sid, scene, title);
  // Do not print meeting titles or transcript content to the engineering log.
  ESP_LOGI(TAG, "LY|REC|action=start result=ok session=%s scene=%d", sid, scene);
  return APP_ERR_NONE;
}

static void hook_stop_recording(app_close_reason_t reason) {
  ESP_LOGI(TAG, "LY|REC|action=stop phase=drain reason=%d", (int)reason);
  audio_pdm_stop();
  sd_session_request_close(reason);           // 排空后闭合;完成事件驱动“已保存”页面
  power_set_low_noise(false);
}

static app_error_t hook_recording_close_status(void) {
  app_error_t status = sd_session_close_status();
  ESP_LOGW(TAG, "LY|REC|phase=close_poll status=%d", (int)status);
  return status;
}

static void hook_mark_point(app_mark_kind_t kind, int64_t at_ms) {
  static const char *K[] = {"important", "fav", "todo"};
  esp_err_t error = sd_session_mark(K[kind], at_ms);
  ESP_LOGI(TAG, "LY|MARK|kind=%s at_ms=%lld result=%s", K[kind],
           (long long)at_ms, esp_err_to_name(error));
}

static bool hook_todo_start(void) {
  if (!s_audio_ready || !storage_sd_mounted() || todo_capture_active()) return false;
  char todo_id[LUOYE_TODO_ID_BYTES];
  if (todo_generate_id(todo_id, sizeof(todo_id)) != ESP_OK) return false;
  s_todo_started_audio = !sd_session_is_open();
  if (s_todo_started_audio) {
    power_set_low_noise(true);
    if (audio_pdm_start() != ESP_OK) {
      power_set_low_noise(false);
      s_todo_started_audio = false;
      return false;
    }
  }
  uint32_t generation = net_binding_generation();
  esp_err_t result = todo_capture_begin(todo_id, generation);
  if (result != ESP_OK && s_todo_started_audio) {
    audio_pdm_stop();
    power_set_low_noise(false);
    s_todo_started_audio = false;
  }
  if (result == ESP_OK) {
    ESP_LOGI(TAG, "LY|TODO|event=capture_start id=%s binding=%lu",
             todo_id, (unsigned long)generation);
  }
  return result == ESP_OK;
}

static bool hook_todo_end(bool saved) {
  luoye_todo_item_t item;
  esp_err_t result = todo_capture_end(saved, &item);
  if (s_todo_started_audio) {
    audio_pdm_stop();
    power_set_low_noise(false);
    s_todo_started_audio = false;
  }
  ESP_LOGI(TAG, "LY|TODO|event=capture_end id=%s pcm=%lu result=%s",
           item.id, (unsigned long)item.pcm_bytes, esp_err_to_name(result));
  return result == ESP_OK;
}

static bool hook_todo_action(bool confirm) {
  esp_err_t result = todo_request_action(net_binding_generation(), confirm);
  ESP_LOGI(TAG, "LY|TODO|event=user_action action=%s result=%s",
           confirm ? "confirm" : "cancel", esp_err_to_name(result));
  return result == ESP_OK;
}

static bool hook_sync_request(void) {
  bool accepted = net_request_manual_sync();
  ESP_LOGI(TAG, "LY|SYNC|event=user_request accepted=%d", accepted);
  return accepted;
}

static bool hook_agenda_sync_request(void) {
  bool accepted = net_request_agenda_sync();
  ESP_LOGI(TAG, "LY|AGENDA|event=page_request accepted=%d", accepted);
  return accepted;
}

static void hook_reminder_action(app_reminder_action_t action) {
  luoye_reminder_action_t mapped = action == APP_REMINDER_SNOOZE
                                     ? LUOYE_REMINDER_SNOOZE
                                     : (action == APP_REMINDER_START_MEETING
                                          ? LUOYE_REMINDER_START_MEETING
                                          : LUOYE_REMINDER_DISMISS);
  agenda_reminder_action(mapped, action == APP_REMINDER_SNOOZE ? 10 : 0);
}

static void hook_power_on(void) {
  if (s_ui_ready) epd_power(true);
  led_ctrl_self_test();
}

static void hook_power_off(void) {
  if (s_ui_ready) {
    ui_wait_idle(6000);                      // 等「已关机」页画完
    epd_deep_sleep();
    epd_power(false);
  }
  power_enter_off();                         // 无提醒不返回;有提醒时仅 RTC/长按 REC 返回
  if (gpio_get_level(PIN_RTC_INT) == 0) {
    rtc_clear_alarm();
    app_post(APP_EV_RTC_ALARM, 0);
  }
  // REC 长按由仍在运行的 keys 任务投递，不能在这里重复投递。
}

static void hook_snooze(void) { rtc_snooze_minutes(10); }
static int64_t hook_now_ms(void) { return esp_timer_get_time() / 1000; }

// ---------- 深睡唤醒:长按 REC 3s 才真正开机(模拟器语义) ----------
static void check_power_on_gate(void) {
  // 深睡前按键被切到 RTC mux(power_enter_off),唤醒后必须归还数字 GPIO,
  // 否则 gpio_get_level 读不到 → 三键失灵。非深睡启动时 deinit 也无害。
  const int keys[] = {PIN_KEY_REC, PIN_KEY_MARK, PIN_KEY_BACK};
  for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) rtc_gpio_deinit(keys[i]);

  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_EXT1) return;   // 正常上电/复位直接开机
  ESP_LOGI(TAG, "LY|POWER_GATE|wake=EXT1 action=wait_rec_hold hold_ms=3000");
  gpio_config_t cfg = {
    .pin_bit_mask = 1ULL << PIN_KEY_REC,
    .mode = GPIO_MODE_INPUT,
    .pull_up_en = GPIO_PULLUP_ENABLE,
  };
  gpio_config(&cfg);
  for (int i = 0; i < 300; i++) {            // 需持续按住 3s
    if (gpio_get_level(PIN_KEY_REC) != 0) {  // 提前松开 → 回深睡
      ESP_LOGI(TAG, "LY|POWER_GATE|result=rejected action=deep_sleep");
      const uint64_t mask = (1ULL << PIN_KEY_REC) | (1ULL << PIN_KEY_MARK) | (1ULL << PIN_KEY_BACK);
      esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
      esp_deep_sleep_start();
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  ESP_LOGI(TAG, "LY|POWER_GATE|result=accepted action=boot");
}

static bool event_is_user_activity(app_event_t event) {
  return event == APP_EV_KEY_REC_SHORT || event == APP_EV_KEY_MARK_SHORT ||
         event == APP_EV_KEY_BACK_SHORT || event == APP_EV_KEY_REC_LONG ||
         event == APP_EV_KEY_REC_RELEASE || event == APP_EV_KEY_BACK_LONG ||
         event == APP_EV_KEY_MARK_HOLD || event == APP_EV_KEY_MARK_RELEASE;
}

static bool idle_sleep_allowed(void) {
  const app_state_t *state = app_state_get();
  return state && state->mode == APP_MODE_STANDBY &&
         (state->overlay == APP_OV_NONE ||
          state->overlay == APP_OV_REMINDER) && !state->locked &&
         state->sync != APP_SYNC_RUNNING &&
         !sd_session_is_open() && !todo_capture_active() && net_can_idle();
}

static uint64_t idle_next_minute_us(void) {
  rtc_time_t value;
  if (rtc_get_time(&value) == ESP_OK && value.sec >= 0 && value.sec <= 59) {
    int seconds = 60 - value.sec;
    if (seconds < 1 || seconds > 60) seconds = 60;
    return (uint64_t)seconds * 1000000ULL;
  }
  return IDLE_WAKE_FALLBACK_US;
}

static bool idle_key_is_down(void) {
  return gpio_get_level(PIN_KEY_REC) == 0 ||
         gpio_get_level(PIN_KEY_MARK) == 0 ||
         gpio_get_level(PIN_KEY_BACK) == 0;
}

static void idle_disable_gpio_wake(void) {
  const int wake_pins[] = {
    PIN_KEY_REC, PIN_KEY_MARK, PIN_KEY_BACK, PIN_RTC_INT,
  };
  for (size_t i = 0; i < sizeof(wake_pins) / sizeof(wake_pins[0]); ++i) {
    gpio_wakeup_disable(wake_pins[i]);
  }
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
}

static bool idle_half_hour(void) {
  time_t now = time(NULL);
  struct tm utc = {0};
  return now >= 1577836800 && gmtime_r(&now, &utc) &&
         (utc.tm_min % 30) == 0;
}

static bool idle_stop_agenda_maintenance(void) {
  int64_t deadline = esp_timer_get_time() / 1000 +
                     IDLE_AGENDA_STOP_GRACE_MS;
  esp_err_t error;
  do {
    error = net_idle_agenda_maintenance_stop();
    if (error == ESP_OK) return true;
    vTaskDelay(pdMS_TO_TICKS(50));
  } while (esp_timer_get_time() / 1000 < deadline);
  ESP_LOGW(TAG, "LY|IDLE_AGENDA|state=stop_timeout action=full_resume");
  net_idle_resume();
  return false;
}

/* Returns true only for a full/user wake.  Silent minute, half-hour and RTC
   maintenance must not reset the user-idle deadline. */
static bool idle_light_sleep(void) {
  if (!idle_sleep_allowed() || net_idle_suspend() != ESP_OK) return false;
  ui_wait_idle(6000);
  ESP_LOGI(TAG,
           "LY|IDLE|state=enter mode=light wifi=off keys=REC,MARK,BACK rtc_int=on");
  for (;;) {
    const int wake_pins[] = {
      PIN_KEY_REC, PIN_KEY_MARK, PIN_KEY_BACK, PIN_RTC_INT,
    };
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    for (size_t i = 0; i < sizeof(wake_pins) / sizeof(wake_pins[0]); ++i) {
      gpio_wakeup_enable(wake_pins[i], GPIO_INTR_LOW_LEVEL);
    }
    esp_sleep_enable_gpio_wakeup();
    esp_sleep_enable_timer_wakeup(idle_next_minute_us());
    esp_err_t sleep_error = esp_light_sleep_start();
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    bool rtc_irq = gpio_get_level(PIN_RTC_INT) == 0;
    bool key = idle_key_is_down();
    idle_disable_gpio_wake();
    ESP_LOGI(TAG, "LY|IDLE|state=wake cause=%d key=%d rtc=%d result=%s",
             (int)cause, key, rtc_irq, esp_err_to_name(sleep_error));

    if (key) {
      net_idle_resume();
      net_request_agenda_sync();
      return true;
    }
    if (rtc_irq) {
      /* RTC reminders are entirely local.  Keep Wi-Fi suspended and let the
         app loop paint the reminder before returning to light sleep. */
      app_post(APP_EV_RTC_ALARM, 0);
      return false;
    }
    if (sleep_error != ESP_OK || cause != ESP_SLEEP_WAKEUP_TIMER) {
      net_idle_resume();
      return true;
    }

    esp_err_t rtc_error = rtc_restore_system();
    const app_state_t *state = app_state_get();
    bool clock_visible = state && state->mode == APP_MODE_STANDBY &&
                         state->overlay == APP_OV_NONE && state->page == 0 &&
                         state->charging == APP_CHG_NONE;
    const char *refresh = "none";
    bool half_hour = idle_half_hour();
    if (half_hour) {
      bool maintenance = net_idle_agenda_maintenance_start();
      bool agenda_changed = false;
      if (maintenance) {
        int64_t deadline = esp_timer_get_time() / 1000 +
                           IDLE_AGENDA_MAINTENANCE_MS;
        while (!net_idle_agenda_maintenance_done(&agenda_changed) &&
               esp_timer_get_time() / 1000 < deadline) {
          if (idle_key_is_down()) {
            net_idle_resume();
            net_request_agenda_sync();
            return true;
          }
          if (gpio_get_level(PIN_RTC_INT) == 0) {
            if (!idle_stop_agenda_maintenance()) return true;
            app_post(APP_EV_RTC_ALARM, 0);
            return false;
          }
          vTaskDelay(pdMS_TO_TICKS(50));
        }
        if (!idle_stop_agenda_maintenance()) return true;
      }
      /* Coalesce the half-hour ghost cleanup with the newly downloaded
         agenda, so a changed agenda never causes two consecutive repaints. */
      ui_request_render(APP_RENDER_FULL);
      ui_wait_idle(6000);
      refresh = agenda_changed ? "full+agenda" : "full";
    } else if (clock_visible) {
      ui_request_render(APP_RENDER_CLOCK_PARTIAL);
      ui_wait_idle(6000);
      refresh = "clock+battery-partial";
    }
    ESP_LOGI(TAG, "LY|IDLE|state=minute_update rtc=%s refresh=%s wifi=off",
             esp_err_to_name(rtc_error), refresh);
    if (!idle_sleep_allowed()) {
      net_idle_resume();
      return true;
    }
    /* Give the single app event loop a chance to drain power/network events
       after every silent wake.  The unchanged idle deadline sends us back to
       sleep immediately, without treating this as user activity. */
    return false;
  }
}

static void dispatch_app_message(const app_msg_t *message) {
  if (!message) return;
  if (message->ev == APP_EV_RTC_ALARM) {
    luoye_agenda_item_t due;
    if (!agenda_take_due((int64_t)time(NULL), &due)) {
      agenda_schedule_next();
      return;
    }
    app_state_set_reminder(due.title);
  }
  app_state_handle(message->ev, message->arg);
}

void app_main(void) {
  luoye_diag_reset();
  luoye_build_log_boot();
  log_main_stack("boot");
  check_power_on_gate();

  // Persist RTC and server timestamps in UTC. Account timezone only affects UI.
  setenv("TZ", "UTC0", 1);
  tzset();

  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    esp_err_t erase_err = nvs_flash_erase();
    err = erase_err == ESP_OK ? nvs_flash_init() : erase_err;
  }
  luoye_diag_set(LUOYE_SUBSYS_NVS,
                 err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 err, err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_NVS_INIT);

  s_q = xQueueCreate(32, sizeof(app_msg_t));
  err = s_q ? ESP_OK : ESP_ERR_NO_MEM;
  luoye_diag_set(LUOYE_SUBSYS_EVENT_QUEUE,
                 err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 err, err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_EVENT_QUEUE_CREATE);
  ESP_ERROR_CHECK(err);

  const esp_timer_create_args_t t0 = {.callback = timer_cb, .arg = (void *)APP_TIMER_OVERLAY, .name = "sm_ov"};
  const esp_timer_create_args_t t1 = {.callback = timer_cb, .arg = (void *)APP_TIMER_ENDING, .name = "sm_end"};
  const esp_timer_create_args_t t2 = {.callback = timer_cb, .arg = (void *)APP_TIMER_CLOSE_POLL, .name = "sm_close"};
  const esp_timer_create_args_t t3 = {.callback = timer_cb, .arg = (void *)APP_TIMER_PAIRING, .name = "sm_pair"};
  esp_err_t timer_err = esp_timer_create(&t0, &s_timers[APP_TIMER_OVERLAY]);
  if (timer_err == ESP_OK) timer_err = esp_timer_create(&t1, &s_timers[APP_TIMER_ENDING]);
  if (timer_err == ESP_OK) timer_err = esp_timer_create(&t2, &s_timers[APP_TIMER_CLOSE_POLL]);
  if (timer_err == ESP_OK) timer_err = esp_timer_create(&t3, &s_timers[APP_TIMER_PAIRING]);
  luoye_diag_set(LUOYE_SUBSYS_TIMERS,
                 timer_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 timer_err, timer_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_TIMER_CREATE);
  ESP_ERROR_CHECK(timer_err);

  // 状态机回调装配
  static const app_hooks_t hooks = {
    .now_ms = hook_now_ms,
    .render = ui_request_render,
    .start_recording = hook_start_recording,
    .stop_recording = hook_stop_recording,
    .set_paused = audio_pdm_set_muted,
    .mark_point = hook_mark_point,
    .mark_flash = led_ctrl_mark_flash,
    .todo_capture_start = hook_todo_start,
    .todo_capture_end = hook_todo_end,
    .todo_action = hook_todo_action,
    .sync_request = hook_sync_request,
    .agenda_sync_request = hook_agenda_sync_request,
    .reminder_action = hook_reminder_action,
    .enter_pairing = net_enter_pairing,
    .exit_pairing = net_exit_pairing,
    .power_on = hook_power_on,
    .power_off = hook_power_off,
    .rtc_snooze_10min = hook_snooze,
    .recording_close_status = hook_recording_close_status,
    .set_timer = hook_set_timer,
    .cancel_timer = hook_cancel_timer,
    .storage_format = hook_storage_format,
  };
  app_state_init(&hooks);

  // 子系统(失败不阻塞开机:无卡可看时间,无网先写卡)
  esp_err_t led_err = led_ctrl_init();
  luoye_diag_set(LUOYE_SUBSYS_LED,
                 led_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 led_err, led_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_LED_INIT);

  log_main_stack("before_ui");
  esp_err_t ui_err = ui_init();
  s_ui_ready = ui_err == ESP_OK;
  luoye_diag_set(LUOYE_SUBSYS_UI,
                 ui_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 ui_err, ui_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_UI_INIT);
  log_main_stack("after_ui");

  esp_err_t keys_err = input_keys_init(app_post);
  luoye_diag_set(LUOYE_SUBSYS_KEYS,
                 keys_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 keys_err, keys_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_KEYS_INIT);

  esp_err_t power_err = power_mgr_init(app_post);
  luoye_diag_set(LUOYE_SUBSYS_POWER,
                 power_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 power_err, power_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_POWER_INIT);
  if (power_err == ESP_OK) rtc_restore_system();

  esp_err_t storage_err = storage_sd_init(app_post);
  luoye_diag_set(LUOYE_SUBSYS_STORAGE,
                 storage_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_DEGRADED,
                 storage_err, storage_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_STORAGE_INIT);
  log_main_stack("after_storage");

  esp_err_t audio_err = audio_pdm_init();
  s_audio_ready = audio_err == ESP_OK;
  luoye_diag_set(LUOYE_SUBSYS_AUDIO,
                 audio_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_FAILED,
                 audio_err, audio_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_AUDIO_INIT);

  esp_err_t agenda_err = storage_err == ESP_OK && power_err == ESP_OK
                           ? agenda_todo_init() : ESP_ERR_INVALID_STATE;
  ESP_LOGI(TAG, "LY|INIT|subsystem=agenda_todo status=%s esp=%s",
           agenda_err == ESP_OK ? "OK" : "DEGRADED",
           esp_err_to_name(agenda_err));

  esp_err_t net_err = net_uploader_init(app_post);
  luoye_diag_set(LUOYE_SUBSYS_NETWORK,
                 net_err == ESP_OK ? LUOYE_STATUS_OK : LUOYE_STATUS_DEGRADED,
                 net_err, net_err == ESP_OK ? LUOYE_ERR_NONE : LUOYE_ERR_NETWORK_INIT);

  if (led_err == ESP_OK) led_ctrl_self_test();
  if (ui_err == ESP_OK) ui_request_render(APP_RENDER_FULL);  // 开机首帧全刷
  luoye_diag_log_snapshot();
  log_main_stack("boot_ready");

  // 事件主循环:所有状态变更在本任务串行执行
  app_msg_t m;
  int64_t last_user_activity_ms = esp_timer_get_time() / 1000;
  for (;;) {
    if (xQueueReceive(s_q, &m, pdMS_TO_TICKS(1000)) == pdPASS) {
      if (event_is_user_activity(m.ev)) {
        last_user_activity_ms = esp_timer_get_time() / 1000;
        if (net_idle_is_suspended()) {
          net_idle_resume();
          net_request_agenda_sync();
        }
      }
      dispatch_app_message(&m);
      continue;
    }
    int64_t now_ms = esp_timer_get_time() / 1000;
    if (now_ms - last_user_activity_ms >= IDLE_SLEEP_AFTER_MS &&
        idle_sleep_allowed()) {
      if (idle_light_sleep()) {
        last_user_activity_ms = esp_timer_get_time() / 1000;
      }
    }
  }
}
