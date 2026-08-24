#include "app_state.h"
#include <string.h>

static app_state_t S;
static app_hooks_t H;

#define CALL(fn, ...) do { if (H.fn) H.fn(__VA_ARGS__); } while (0)
#define T_ENDING       3200
#define T_CLOSE_POLL   2000
#define CLOSE_POLL_LIMIT 8
#define T_TODO_OK      5000
#define T_SNOOZED      1800
#define T_LOCKED_HINT  1400
#define T_PAIR_BOUND   2500
#define T_SYNC_DONE    2200
#define BATTERY_CRITICAL_PERCENT 3
#define BATTERY_RECORD_MIN_PERCENT 5
#define BATTERY_RECOVER_PERCENT 8

const app_state_t *app_state_get(void) { return &S; }

void app_state_set_reminder(const char *title) {
  strncpy(S.reminder, title ? title : "", sizeof(S.reminder) - 1);
  S.reminder[sizeof(S.reminder) - 1] = '\0';
}

void app_state_init(const app_hooks_t *hooks) {
  if (hooks) H = *hooks;
  else memset(&H, 0, sizeof(H));
  memset(&S, 0, sizeof(S));
  S.mode = APP_MODE_STANDBY;
  S.scene = APP_SCENE_MEETING;
  S.battery = 100;
}

int64_t app_state_elapsed_ms(void) {
  if (S.mode == APP_MODE_CLOSING || S.mode == APP_MODE_ENDING ||
      S.mode == APP_MODE_STORAGE_ERROR) {
    return S.ended_elapsed_ms;
  }
  if (S.mode != APP_MODE_RECORDING || !H.now_ms) return 0;
  int64_t now = H.now_ms();
  int64_t elapsed = now - S.rec_start_ms - S.paused_total_ms;
  if (S.paused) elapsed -= now - S.pause_started_ms;
  return elapsed > 0 ? elapsed : 0;
}

int64_t app_state_todo_elapsed_ms(void) {
  if (S.overlay != APP_OV_TODO_LISTEN || !H.now_ms) return 0;
  int64_t elapsed = H.now_ms() - S.todo_start_ms;
  return elapsed > 0 ? elapsed : 0;
}

uint32_t app_hold_ms(char key) {
  if (key == 'M') return S.mode == APP_MODE_STANDBY ? 600 : 1500;
  if (key == 'B') return 3000;
  if (key == 'R') {
    if (S.mode == APP_MODE_OFF) return 3000;
    return 1500;
  }
  return 1500;
}

static void set_overlay_timed(app_overlay_t overlay, uint32_t ms) {
  S.overlay = overlay;
  CALL(set_timer, APP_TIMER_OVERLAY, ms);
}

static void show_locked_hint(void) {
  set_overlay_timed(APP_OV_LOCKED_HINT, T_LOCKED_HINT);
  CALL(render, APP_RENDER_FAST);
}

static void show_todo_result(int32_t result) {
  S.todo_result_pending = 0;
  if (result == 1) S.overlay = APP_OV_TODO_CONFIRM;
  else if (result == 2) S.overlay = APP_OV_TODO_CREATED;
  else S.overlay = APP_OV_TODO_FAILED;
}

static void enter_error(app_error_t error, bool stop_active_recording) {
  if (S.mode == APP_MODE_RECORDING) S.ended_elapsed_ms = app_state_elapsed_ms();
  if (stop_active_recording) CALL(stop_recording, APP_CLOSE_STORAGE_ERROR);
  S.mode = APP_MODE_STORAGE_ERROR;
  S.error = error == APP_ERR_NONE ? APP_ERR_STORAGE_WRITE : error;
  S.close_reason = APP_CLOSE_STORAGE_ERROR;
  S.overlay = APP_OV_NONE;
  S.paused = false;
  S.locked = false;
  S.storage_settled = !stop_active_recording;
  CALL(cancel_timer, APP_TIMER_CLOSE_POLL);
  CALL(render, APP_RENDER_FAST);
}

static void start_rec(const char *title) {
  if (S.mode == APP_MODE_RECORDING) {
    CALL(render, APP_RENDER_FAST);
    return;
  }
  if (S.mode != APP_MODE_STANDBY) return;
  if (S.battery_low_latched || S.battery < BATTERY_RECORD_MIN_PERCENT) {
    enter_error(APP_ERR_LOW_BATTERY, false);
    return;
  }

  S.mode = APP_MODE_STARTING;
  S.error = APP_ERR_NONE;
  S.close_reason = APP_CLOSE_USER;
  S.overlay = APP_OV_NONE;
  S.page = 0;
  S.paused = false;
  S.locked = false;
  S.pause_started_ms = 0;
  S.paused_total_ms = 0;
  S.marks = S.favs = S.todos = 0;
  S.storage_settled = false;
  S.close_poll_count = 0;
  CALL(render, APP_RENDER_FAST);

  app_error_t result = H.start_recording
                         ? H.start_recording(S.scene, title)
                         : APP_ERR_STORAGE_OPEN;
  if (result != APP_ERR_NONE) {
    enter_error(result, false);
    return;
  }
  S.rec_start_ms = H.now_ms ? H.now_ms() : 0;
  S.mode = APP_MODE_RECORDING;
  S.storage_settled = false;
  if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
  CALL(render, APP_RENDER_FAST);
}

static void begin_close(app_close_reason_t reason) {
  if (S.mode != APP_MODE_RECORDING) return;
  S.ended_elapsed_ms = app_state_elapsed_ms();
  S.mode = APP_MODE_CLOSING;
  S.close_reason = reason;
  S.overlay = APP_OV_NONE;
  S.locked = false;
  S.paused = false;
  CALL(stop_recording, reason);
  S.close_poll_count = 0;
  CALL(set_timer, APP_TIMER_CLOSE_POLL, T_CLOSE_POLL);
  CALL(render, APP_RENDER_FAST);
}

static void close_done(void) {
  if (S.mode != APP_MODE_CLOSING) return;
  S.mode = APP_MODE_ENDING;
  S.error = APP_ERR_NONE;
  S.storage_settled = true;
  CALL(cancel_timer, APP_TIMER_CLOSE_POLL);
  CALL(render, APP_RENDER_FAST);
  CALL(set_timer, APP_TIMER_ENDING, T_ENDING);
}

static void toggle_pause(void) {
  if (!H.now_ms) return;
  if (S.paused) {
    S.paused_total_ms += H.now_ms() - S.pause_started_ms;
    S.pause_started_ms = 0;
    S.paused = false;
  } else {
    S.pause_started_ms = H.now_ms();
    S.paused = true;
  }
  CALL(set_paused, S.paused);
  CALL(render, APP_RENDER_FAST);
}

static void do_power_off(void) {
  if (S.mode != APP_MODE_STANDBY) return;
  S.mode = APP_MODE_OFF;
  S.overlay = APP_OV_NONE;
  CALL(render, APP_RENDER_FAST);
  CALL(power_off);
}

static void todo_hold(void);
static void todo_release(void);

static bool status_page_visible(void) {
  return S.overlay == APP_OV_NONE &&
         ((S.mode == APP_MODE_STANDBY && S.page == 2) ||
          (S.mode == APP_MODE_RECORDING && (S.page & 1U) != 0));
}

static bool home_clock_visible(void) {
  return S.mode == APP_MODE_STANDBY && S.overlay == APP_OV_NONE &&
         S.page == 0 && S.charging == APP_CHG_NONE;
}

static void short_press(char key) {
  if (S.mode == APP_MODE_OFF) return;
  if (S.mode == APP_MODE_STARTING || S.mode == APP_MODE_CLOSING ||
      S.mode == APP_MODE_ENDING) return;
  if (S.mode == APP_MODE_STORAGE_ERROR) {
    if (S.error == APP_ERR_STORAGE_FORMAT_REQUIRED ||
        S.error == APP_ERR_STORAGE_FORMATTING ||
        S.error == APP_ERR_STORAGE_FORMAT_FAILED) {
      return;
    }
    if (S.storage_settled && (key == 'R' || key == 'B')) {
      S.mode = S.pairing == APP_PAIR_CLAIM_PENDING
                 ? APP_MODE_PAIRING : APP_MODE_STANDBY;
      S.error = APP_ERR_NONE;
      S.page = 0;
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_TODO_OK || S.overlay == APP_OV_SNOOZED) return;
  if (S.overlay == APP_OV_TODO_LISTEN) return;

  if (S.overlay == APP_OV_SYNC_CONFIRM) {
    if (key == 'M') {
      if (H.sync_request && H.sync_request()) {
        S.sync = APP_SYNC_RUNNING;
        S.overlay = APP_OV_SYNC_PROGRESS;
      } else {
        S.sync = APP_SYNC_FAILED;
        S.overlay = APP_OV_SYNC_FAILED;
      }
      CALL(render, APP_RENDER_FAST);
    } else if (key == 'B') {
      S.sync = APP_SYNC_IDLE;
      S.overlay = APP_OV_NONE;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_SYNC_PROGRESS) {
    if (key == 'B') {
      S.overlay = APP_OV_NONE;  // 上传继续，BACK 只返回主页
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_SYNC_DONE || S.overlay == APP_OV_SYNC_FAILED) {
    if (key == 'M' || key == 'B') {
      S.overlay = APP_OV_NONE;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }

  if (S.overlay == APP_OV_TODO_CONFIRM) {
    if (key == 'M' || key == 'B') {
      bool saved = H.todo_action && H.todo_action(key == 'M');
      if (saved) set_overlay_timed(APP_OV_TODO_OK, T_TODO_OK);
      else S.overlay = APP_OV_TODO_FAILED;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_TODO_CREATED || S.overlay == APP_OV_TODO_FAILED) {
    if (key == 'M' || key == 'B') {
      S.overlay = APP_OV_NONE;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }

  if (S.overlay == APP_OV_POWER_CONFIRM) {
    if (key == 'R') do_power_off();
    else if (key == 'B') {
      S.overlay = APP_OV_NONE;
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_REMINDER) {
    if (key == 'R') {
      CALL(reminder_action, APP_REMINDER_START_MEETING);
      S.overlay = APP_OV_NONE;
      if (S.mode == APP_MODE_RECORDING) {
        CALL(render, APP_RENDER_FAST);
      } else {
        S.scene = APP_SCENE_MEETING;
        start_rec(S.reminder);
      }
    } else if (key == 'B') {
      CALL(reminder_action, APP_REMINDER_DISMISS);
      S.overlay = APP_OV_NONE;
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.mode == APP_MODE_PAIRING) {
    if (S.pairing == APP_PAIR_BOUND) return;
    if (key == 'B') {
      S.mode = APP_MODE_STANDBY;
      S.pairing = APP_PAIR_IDLE;
      S.page = 0;
      CALL(exit_pairing);
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.locked) {
    show_locked_hint();
    return;
  }

  if (S.mode == APP_MODE_STANDBY) {
    if (key == 'R') start_rec(NULL);
    else if (key == 'M') {
      if (S.page != 1) {
        S.page = 1;
        S.agenda_page = 0;
        CALL(agenda_sync_request);
      } else {
        S.agenda_page++;
      }
      CALL(render, APP_RENDER_FAST);
    } else if (key == 'B') {
      S.page = S.page == 0 ? 2 : 0;
      S.agenda_page = 0;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }

  if (S.mode == APP_MODE_RECORDING) {
    if (key == 'R') toggle_pause();
    else if (key == 'M') {
      S.marks++;
      CALL(mark_point, APP_MARK_IMPORTANT, app_state_elapsed_ms());
      CALL(mark_flash);
    }
    else if (key == 'B') {
      S.page ^= 1U;
      CALL(render, APP_RENDER_FAST);
    }
  }
}

static void long_press(char key) {
  if (S.mode == APP_MODE_STORAGE_ERROR) {
    if (S.error == APP_ERR_STORAGE_FORMAT_REQUIRED && key == 'R') {
      S.error = APP_ERR_STORAGE_FORMATTING;
      S.storage_settled = false;
      CALL(render, APP_RENDER_FAST);
      bool started = H.storage_format && H.storage_format();
      if (!started) {
        S.error = APP_ERR_STORAGE_FORMAT_FAILED;
        S.storage_settled = true;
        CALL(render, APP_RENDER_FAST);
      }
    }
    return;
  }
  if (S.mode == APP_MODE_STARTING || S.mode == APP_MODE_CLOSING ||
      S.mode == APP_MODE_ENDING) return;
  if (S.overlay == APP_OV_TODO_LISTEN || S.overlay == APP_OV_TODO_OK ||
      S.overlay == APP_OV_TODO_CONFIRM ||
      S.overlay == APP_OV_TODO_CREATED || S.overlay == APP_OV_TODO_FAILED ||
      S.overlay == APP_OV_SYNC_CONFIRM ||
      S.overlay == APP_OV_SYNC_PROGRESS || S.overlay == APP_OV_SYNC_DONE ||
      S.overlay == APP_OV_SYNC_FAILED ||
      S.overlay == APP_OV_SNOOZED) return;

  if (S.overlay == APP_OV_REMINDER) {
    if (key == 'B') {
      CALL(reminder_action, APP_REMINDER_SNOOZE);
      set_overlay_timed(APP_OV_SNOOZED, T_SNOOZED);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }

  if (S.mode == APP_MODE_OFF) {
    if (key == 'R') {
      S.mode = APP_MODE_STANDBY;
      S.overlay = APP_OV_NONE;
      S.page = 0;
      CALL(power_on);
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.overlay == APP_OV_POWER_CONFIRM) return;
  if (S.locked) {
    if (key == 'B') {
      S.locked = false;
      S.overlay = APP_OV_NONE;
      if (S.todo_result_pending) show_todo_result(S.todo_result_pending);
      CALL(render, APP_RENDER_FAST);
    } else {
      show_locked_hint();
    }
    return;
  }
  if (S.mode == APP_MODE_STANDBY) {
    if (key == 'R') {
      S.sync = APP_SYNC_IDLE;
      S.overlay = APP_OV_SYNC_CONFIRM;
      CALL(render, APP_RENDER_FAST);
    } else if (key == 'M') {
      todo_hold();
    } else if (key == 'B') {
      S.mode = APP_MODE_PAIRING;
      S.page = 0;
      CALL(enter_pairing);
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (S.mode == APP_MODE_RECORDING) {
    if (key == 'R') begin_close(APP_CLOSE_USER);
    else if (key == 'B') {
      S.locked = true;
      S.overlay = APP_OV_NONE;
      CALL(render, APP_RENDER_FAST);
    }
  }
}

static void todo_hold(void) {
  if (S.mode != APP_MODE_STANDBY) return;
  if (S.battery_low_latched || S.battery < BATTERY_RECORD_MIN_PERCENT) {
    enter_error(APP_ERR_LOW_BATTERY, false);
    return;
  }
  if (S.overlay == APP_OV_REMINDER || S.overlay == APP_OV_POWER_CONFIRM) return;
  if (S.locked) {
    show_locked_hint();
    return;
  }
  if (!H.todo_capture_start || !H.todo_capture_start()) {
    S.overlay = APP_OV_TODO_FAILED;
    CALL(render, APP_RENDER_FAST);
    return;
  }
  S.todo_start_ms = H.now_ms ? H.now_ms() : 0;
  S.overlay = APP_OV_TODO_LISTEN;
  CALL(render, APP_RENDER_FAST);
}

static void todo_release(void) {
  if (S.overlay != APP_OV_TODO_LISTEN) return;
  bool saved = H.todo_capture_end && H.todo_capture_end(true);
  S.todo_start_ms = 0;
  if (saved) {
    S.todos++;
    CALL(mark_point, APP_MARK_TODO, app_state_elapsed_ms());
    set_overlay_timed(APP_OV_TODO_OK, T_TODO_OK);
  } else {
    S.overlay = APP_OV_TODO_FAILED;
  }
  CALL(render, APP_RENDER_FAST);
}

static void rtc_alarm(void) {
  if (S.mode == APP_MODE_STARTING || S.mode == APP_MODE_CLOSING ||
      S.mode == APP_MODE_ENDING || S.mode == APP_MODE_STORAGE_ERROR ||
      S.overlay == APP_OV_TODO_LISTEN) {
    S.reminder_pending = true;
    return;
  }
  if (S.mode == APP_MODE_OFF) {
    S.mode = APP_MODE_STANDBY;
    CALL(power_on);
  }
  S.overlay = APP_OV_REMINDER;
  S.reminder_pending = false;
  CALL(render, APP_RENDER_FAST);
}

static void on_timer(app_timer_id_t id) {
  if (id == APP_TIMER_PAIRING) {
    if (S.mode == APP_MODE_PAIRING && S.pairing == APP_PAIR_BOUND) {
      S.mode = APP_MODE_STANDBY;
      S.page = 0;
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  if (id == APP_TIMER_CLOSE_POLL) {
    if (S.mode != APP_MODE_CLOSING) return;
    app_error_t status = H.recording_close_status
                           ? H.recording_close_status() : APP_ERR_BUSY;
    if (status == APP_ERR_NONE) {
      close_done();
    } else if (status != APP_ERR_BUSY) {
      enter_error(status, false);
    } else if (++S.close_poll_count >= CLOSE_POLL_LIMIT) {
      enter_error(APP_ERR_STORAGE_TIMEOUT, false);
      S.storage_settled = false;
    } else {
      CALL(set_timer, APP_TIMER_CLOSE_POLL, T_CLOSE_POLL);
    }
    return;
  }
  if (id == APP_TIMER_ENDING) {
    if (S.mode == APP_MODE_ENDING) {
      S.mode = S.pairing == APP_PAIR_CLAIM_PENDING
                 ? APP_MODE_PAIRING : APP_MODE_STANDBY;
      S.page = 0;
      if (S.reminder_pending) {
        S.overlay = APP_OV_REMINDER;
        S.reminder_pending = false;
      } else if (S.todo_result_pending) {
        show_todo_result(S.todo_result_pending);
      }
      CALL(render, APP_RENDER_FAST);
    }
    return;
  }
  switch (S.overlay) {
    case APP_OV_SNOOZED:
    case APP_OV_TODO_OK:
      S.overlay = APP_OV_NONE;
      if (S.reminder_pending) {
        S.overlay = APP_OV_REMINDER;
        S.reminder_pending = false;
      } else if (S.todo_result_pending) {
        show_todo_result(S.todo_result_pending);
      }
      CALL(render, APP_RENDER_FAST);
      break;
    case APP_OV_LOCKED_HINT:
      S.overlay = APP_OV_NONE;
      CALL(render, APP_RENDER_FAST);
      break;
    case APP_OV_SYNC_DONE:
      S.overlay = APP_OV_NONE;
      S.sync = APP_SYNC_IDLE;
      CALL(render, APP_RENDER_FAST);
      break;
    default:
      break;
  }
}

void app_state_handle(app_event_t event, int32_t arg) {
  switch (event) {
    case APP_EV_KEY_REC_SHORT:    short_press('R'); break;
    case APP_EV_KEY_MARK_SHORT:   short_press('M'); break;
    case APP_EV_KEY_BACK_SHORT:   short_press('B'); break;
    case APP_EV_KEY_REC_LONG:     long_press('R'); break;
    case APP_EV_KEY_REC_RELEASE:  break;
    case APP_EV_KEY_BACK_LONG:    long_press('B'); break;
    case APP_EV_KEY_MARK_HOLD:    long_press('M'); break;
    case APP_EV_KEY_MARK_RELEASE: todo_release(); break;
    case APP_EV_RTC_ALARM:        rtc_alarm(); break;
    case APP_EV_TIMER:            on_timer((app_timer_id_t)arg); break;
    case APP_EV_SESSION_CLOSE_DONE:
      if (S.mode == APP_MODE_CLOSING) {
        close_done();
      } else if (S.mode == APP_MODE_STORAGE_ERROR &&
                 S.error == APP_ERR_STORAGE_TIMEOUT) {
        S.mode = APP_MODE_ENDING;
        S.error = APP_ERR_NONE;
        S.storage_settled = true;
        CALL(render, APP_RENDER_FAST);
        CALL(set_timer, APP_TIMER_ENDING, T_ENDING);
      }
      break;
    case APP_EV_SESSION_SETTLED:
      if (S.mode == APP_MODE_STORAGE_ERROR) {
        S.storage_settled = true;
        CALL(render, APP_RENDER_FAST);
      }
      break;
    case APP_EV_STORAGE_ERROR:
      if (S.mode == APP_MODE_STARTING || S.mode == APP_MODE_RECORDING) {
        enter_error((app_error_t)arg, S.mode == APP_MODE_RECORDING);
      } else if (S.mode == APP_MODE_CLOSING) {
        enter_error((app_error_t)arg, false);
        S.storage_settled = false;
      } else if (S.mode == APP_MODE_STORAGE_ERROR && !S.storage_settled) {
        S.error = (app_error_t)arg;
      } else if (S.mode == APP_MODE_STANDBY &&
                 (arg == APP_ERR_STORAGE_FORMAT_REQUIRED ||
                  arg == APP_ERR_STORAGE_FORMAT_FAILED)) {
        enter_error((app_error_t)arg, false);
      }
      break;
    case APP_EV_NET_CHANGE:
      if (S.online != (bool)arg) {
        S.online = (bool)arg;
        if (!S.online) S.cloud_online = false;
        /* A reconnect after light sleep must not repaint the home/recording
           page.  Only the visible status page owns these fields. */
        if (status_page_visible()) CALL(render, APP_RENDER_STATUS_PARTIAL);
      }
      break;
    case APP_EV_CLOUD_CHANGE:
      if (S.cloud_online != (bool)arg) {
        S.cloud_online = (bool)arg;
        if (status_page_visible()) CALL(render, APP_RENDER_STATUS_PARTIAL);
      }
      break;
    case APP_EV_PAIRING_CHANGE:
      S.pairing = (app_pair_state_t)arg;
      if (S.mode == APP_MODE_PAIRING && S.pairing == APP_PAIR_BOUND) {
        CALL(exit_pairing);
        CALL(render, APP_RENDER_FAST);
        CALL(set_timer, APP_TIMER_PAIRING, T_PAIR_BOUND);
      } else if (S.mode == APP_MODE_STANDBY &&
          S.pairing == APP_PAIR_CLAIM_PENDING) {
        S.mode = APP_MODE_PAIRING;
        S.page = 0;
        CALL(render, APP_RENDER_FAST);
      } else if (S.mode == APP_MODE_PAIRING) {
        CALL(render, APP_RENDER_FAST);
      }
      break;
    case APP_EV_CHARGE_CHANGE:
      if (S.charging != (app_charge_t)arg) {
        S.charging = (app_charge_t)arg;
        if (S.charging != APP_CHG_NONE) S.battery_low_latched = false;
        if (S.mode == APP_MODE_STANDBY) CALL(render, APP_RENDER_FAST);
      }
      break;
    case APP_EV_BATTERY:
      {
      uint8_t previous_battery = S.battery;
      S.battery = (uint8_t)(arg < 0 ? 0 : (arg > 100 ? 100 : arg));
      if (S.charging != APP_CHG_NONE || S.battery >= BATTERY_RECOVER_PERCENT) {
        S.battery_low_latched = false;
      } else if (S.battery <= BATTERY_RECORD_MIN_PERCENT) {
        S.battery_low_latched = true;
      }
      if (S.mode == APP_MODE_RECORDING &&
          S.battery <= BATTERY_CRITICAL_PERCENT) {
        begin_close(APP_CLOSE_LOW_BATTERY);
      }
      if (S.battery != previous_battery && status_page_visible()) {
        CALL(render, APP_RENDER_STATUS_PARTIAL);
      }
      }
      break;
    case APP_EV_SD_LOW:
      if (S.sd_low != (bool)arg) {
        S.sd_low = (bool)arg;
        if (status_page_visible()) CALL(render, APP_RENDER_STATUS_PARTIAL);
      }
      break;
    case APP_EV_BACKLOG:
      S.backlog_s = (uint16_t)(arg < 0 ? 0 : arg);
      if (S.overlay == APP_OV_SYNC_PROGRESS) {
        CALL(render, APP_RENDER_STATUS_PARTIAL);
      }
      break;
    case APP_EV_AGENDA_CHANGE:
      S.agenda_page = 0;
      /* A new revision only owns the agenda page.  Other pages pick up the
         cache on their next normal FAST transition, avoiding a disruptive
         full-page repaint while the user is looking elsewhere. */
      if (S.mode == APP_MODE_STANDBY && S.overlay == APP_OV_NONE &&
          S.page == 1) {
        CALL(render, APP_RENDER_FAST);
      }
      break;
    case APP_EV_TIME_SYNC:
      /* A clock correction only owns the clock window on the home page.
         Status/agenda/charging pages must not be repainted by a time event. */
      if (home_clock_visible()) CALL(render, APP_RENDER_CLOCK_PARTIAL);
      break;
    case APP_EV_TODO_RESULT:
      if (S.mode == APP_MODE_STARTING || S.mode == APP_MODE_CLOSING ||
          S.mode == APP_MODE_ENDING || S.mode == APP_MODE_STORAGE_ERROR ||
          S.mode == APP_MODE_OFF || S.mode == APP_MODE_PAIRING || S.locked ||
          S.overlay == APP_OV_REMINDER || S.overlay == APP_OV_TODO_LISTEN ||
          S.overlay == APP_OV_SNOOZED || S.overlay == APP_OV_LOCKED_HINT ||
          S.overlay == APP_OV_POWER_CONFIRM ||
          S.overlay == APP_OV_SYNC_CONFIRM ||
          S.overlay == APP_OV_SYNC_PROGRESS ||
          S.overlay == APP_OV_SYNC_DONE || S.overlay == APP_OV_SYNC_FAILED) {
        S.todo_result_pending = (int8_t)(arg == 1 ? 1 : (arg == 2 ? 2 : -1));
      } else {
        show_todo_result(arg);
        CALL(render, APP_RENDER_FAST);
      }
      break;
    case APP_EV_SYNC_CHANGE:
      S.sync = (app_sync_state_t)arg;
      if (S.sync == APP_SYNC_RUNNING) {
        S.overlay = APP_OV_SYNC_PROGRESS;
      } else if (S.sync == APP_SYNC_DONE) {
        S.overlay = APP_OV_SYNC_DONE;
        CALL(set_timer, APP_TIMER_OVERLAY, T_SYNC_DONE);
      } else if (S.sync == APP_SYNC_FAILED) {
        S.overlay = APP_OV_SYNC_FAILED;
      }
      CALL(render, APP_RENDER_FAST);
      break;
    default:
      break;
  }
}
