#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "app_state.h"

static int64_t now;
static struct { bool armed; int64_t at; } timers[APP_TIMER_COUNT];
static int renders, starts, stops, marks, pair_in, pair_out, power_on_count, power_off_count;
static int todo_starts, todo_ends, todo_actions, reminder_actions;
static int sync_requests, agenda_requests;
static app_render_t last_render;
static bool todo_capture_ok = true, todo_action_ok = true, last_todo_confirm;
static bool sync_request_ok = true;
static app_error_t start_result = APP_ERR_NONE;
static app_error_t close_status_result = APP_ERR_BUSY;
static app_close_reason_t last_close_reason;
static char start_title[64];
static int failures;

#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)
#define ST (app_state_get())
#define EV(e) app_state_handle((e), 0)

static int64_t fake_now(void) { return now; }
static void render(app_render_t kind) { last_render = kind; renders++; }
static app_error_t start_recording(app_scene_t scene, const char *title) {
  (void)scene;
  starts++;
  strncpy(start_title, title ? title : "", sizeof(start_title) - 1);
  return start_result;
}
static void stop_recording(app_close_reason_t reason) {
  stops++;
  last_close_reason = reason;
}
static void set_paused(bool paused) { (void)paused; }
static void mark_point(app_mark_kind_t kind, int64_t at_ms) {
  (void)kind; (void)at_ms; marks++;
}
static void mark_flash(void) {}
static bool todo_start(void) { todo_starts++; return todo_capture_ok; }
static bool todo_end(bool saved) { todo_ends++; return saved && todo_capture_ok; }
static bool todo_action(bool confirm) {
  todo_actions++;
  last_todo_confirm = confirm;
  return todo_action_ok;
}
static bool sync_request(void) { sync_requests++; return sync_request_ok; }
static bool agenda_sync_request(void) { agenda_requests++; return true; }
static void reminder_action(app_reminder_action_t action) {
  (void)action;
  reminder_actions++;
}
static void enter_pairing(void) { pair_in++; }
static void exit_pairing(void) { pair_out++; }
static void power_on(void) { power_on_count++; }
static void power_off(void) { power_off_count++; }
static void snooze(void) {}
static app_error_t close_status(void) { return close_status_result; }
static void set_timer(app_timer_id_t id, uint32_t ms) {
  timers[id].armed = true;
  timers[id].at = now + ms;
}
static void cancel_timer(app_timer_id_t id) { timers[id].armed = false; }

static void advance(int64_t ms) {
  int64_t end = now + ms;
  for (;;) {
    int best = -1;
    for (int i = 0; i < APP_TIMER_COUNT; i++) {
      if (timers[i].armed && timers[i].at <= end &&
          (best < 0 || timers[i].at < timers[best].at)) best = i;
    }
    if (best < 0) break;
    now = timers[best].at;
    timers[best].armed = false;
    app_state_handle(APP_EV_TIMER, best);
  }
  now = end;
}

static void close_successfully(void) {
  EV(APP_EV_KEY_REC_LONG);
  CHECK(ST->mode == APP_MODE_CLOSING);
  EV(APP_EV_SESSION_CLOSE_DONE);
  CHECK(ST->mode == APP_MODE_ENDING);
  advance(3200);
  CHECK(ST->mode == APP_MODE_STANDBY);
}

int main(void) {
  const app_hooks_t hooks = {
    .now_ms = fake_now,
    .render = render,
    .start_recording = start_recording,
    .stop_recording = stop_recording,
    .set_paused = set_paused,
    .mark_point = mark_point,
    .mark_flash = mark_flash,
    .todo_capture_start = todo_start,
    .todo_capture_end = todo_end,
    .todo_action = todo_action,
    .sync_request = sync_request,
    .agenda_sync_request = agenda_sync_request,
    .reminder_action = reminder_action,
    .enter_pairing = enter_pairing,
    .exit_pairing = exit_pairing,
    .power_on = power_on,
    .power_off = power_off,
    .rtc_snooze_10min = snooze,
    .recording_close_status = close_status,
    .set_timer = set_timer,
    .cancel_timer = cancel_timer,
  };
  app_state_init(&hooks);
  CHECK(ST->mode == APP_MODE_STANDBY);
  /* Settings toggles status/home; todo key enters and advances agenda pages. */
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 2 && last_render == APP_RENDER_FAST);
  app_state_handle(APP_EV_NET_CHANGE, 1);
  CHECK(ST->online && last_render == APP_RENDER_STATUS_PARTIAL);
  int renders_before_hidden_time = renders;
  EV(APP_EV_TIME_SYNC);
  CHECK(renders == renders_before_hidden_time);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->mode == APP_MODE_STANDBY && ST->page == 0 &&
        last_render == APP_RENDER_FAST);
  int renders_before_home_time = renders;
  EV(APP_EV_TIME_SYNC);
  CHECK(renders == renders_before_home_time + 1 &&
        last_render == APP_RENDER_CLOCK_PARTIAL);

  /* The charging page repaints only when entering/leaving charging or when
     the displayed percentage crosses a five-percent bucket.  Charge Done is
     coalesced with the following 100% event to avoid two FAST flashes. */
  app_state_handle(APP_EV_BATTERY, 82);
  int renders_before_charge = renders;
  app_state_handle(APP_EV_CHARGE_CHANGE, APP_CHG_CHARGING);
  CHECK(renders == renders_before_charge + 1 &&
        last_render == APP_RENDER_FAST);
  int renders_before_charge_percent = renders;
  app_state_handle(APP_EV_BATTERY, 83);
  app_state_handle(APP_EV_BATTERY, 84);
  CHECK(renders == renders_before_charge_percent);
  app_state_handle(APP_EV_BATTERY, 85);
  CHECK(renders == renders_before_charge_percent + 1 &&
        last_render == APP_RENDER_FAST);
  int renders_before_full = renders;
  app_state_handle(APP_EV_CHARGE_CHANGE, APP_CHG_FULL);
  CHECK(renders == renders_before_full);
  app_state_handle(APP_EV_BATTERY, 100);
  CHECK(renders == renders_before_full + 1 &&
        last_render == APP_RENDER_FAST);
  int renders_before_unplug = renders;
  app_state_handle(APP_EV_CHARGE_CHANGE, APP_CHG_NONE);
  CHECK(renders == renders_before_unplug + 1 &&
        last_render == APP_RENDER_FAST);
  app_state_handle(APP_EV_BATTERY, 80);

  int renders_before_hidden_network = renders;
  app_state_handle(APP_EV_NET_CHANGE, 0);
  CHECK(!ST->online && renders == renders_before_hidden_network);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(ST->page == 1 && ST->agenda_page == 0 && agenda_requests == 1);
  int renders_before_agenda_change = renders;
  app_state_handle(APP_EV_AGENDA_CHANGE, 7);
  CHECK(renders == renders_before_agenda_change + 1 &&
        last_render == APP_RENDER_FAST);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(ST->page == 1 && ST->agenda_page == 1 && agenda_requests == 1);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 0 && ST->agenda_page == 0);
  renders_before_agenda_change = renders;
  app_state_handle(APP_EV_AGENDA_CHANGE, 8);
  CHECK(renders == renders_before_agenda_change);

  /* STARTING is internal and start is accepted only after storage hook succeeds. */
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_RECORDING && starts == 1);
  advance(10000);
  EV(APP_EV_KEY_REC_LONG);
  CHECK(ST->mode == APP_MODE_CLOSING && stops == 1);
  CHECK(last_close_reason == APP_CLOSE_USER);
  advance(10000);
  CHECK(ST->mode == APP_MODE_CLOSING); /* no fake "saved" before close-done */
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_CLOSING); /* new recording is blocked */
  EV(APP_EV_SESSION_CLOSE_DONE);
  CHECK(ST->mode == APP_MODE_ENDING && ST->ended_elapsed_ms == 10000);
  advance(3200);
  CHECK(ST->mode == APP_MODE_STANDBY);

  /* Open failures become an explicit error page. */
  start_result = APP_ERR_NO_SD;
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR && ST->error == APP_ERR_NO_SD);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->mode == APP_MODE_STANDBY);
  start_result = APP_ERR_NONE;

  /* Runtime short-write/removal stops fake recording immediately. */
  EV(APP_EV_KEY_REC_SHORT);
  advance(500);
  app_state_handle(APP_EV_STORAGE_ERROR, APP_ERR_STORAGE_WRITE);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR);
  CHECK(ST->error == APP_ERR_STORAGE_WRITE);
  CHECK(stops == 2 && last_close_reason == APP_CLOSE_STORAGE_ERROR);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR); /* still salvaging, cannot leave */
  EV(APP_EV_SESSION_SETTLED);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_STANDBY);

  /* A close-time fsync error must never pass through ENDING/saved. */
  EV(APP_EV_KEY_REC_SHORT);
  EV(APP_EV_KEY_REC_LONG);
  app_state_handle(APP_EV_STORAGE_ERROR, APP_ERR_STORAGE_SYNC);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR && ST->error == APP_ERR_STORAGE_SYNC);
  EV(APP_EV_SESSION_CLOSE_DONE);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR);
  EV(APP_EV_SESSION_SETTLED);
  EV(APP_EV_KEY_BACK_SHORT);

  /* Display percentage never blocks use or stops an active recording.  Only
     the power manager's debounced physical-voltage emergency closes safely. */
  app_state_handle(APP_EV_BATTERY, 80);
  EV(APP_EV_KEY_REC_SHORT);
  app_state_handle(APP_EV_BATTERY, 3);
  CHECK(ST->mode == APP_MODE_RECORDING && ST->battery == 3);
  app_state_handle(APP_EV_BATTERY, 0);
  CHECK(ST->mode == APP_MODE_RECORDING && ST->battery == 0);
  app_state_handle(APP_EV_BATTERY_CRITICAL, 2995);
  CHECK(ST->mode == APP_MODE_CLOSING);
  CHECK(last_close_reason == APP_CLOSE_LOW_BATTERY);
  EV(APP_EV_SESSION_CLOSE_DONE);
  advance(3200);

  /* Even a displayed 0% can start recording; voltage, not percentage, owns
     the final data-integrity boundary. */
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_RECORDING && ST->battery == 0);
  close_successfully();
  app_state_handle(APP_EV_BATTERY, 80);

  /* WiFi reachability and cloud/API readiness are distinct states. */
  app_state_handle(APP_EV_NET_CHANGE, 1);
  CHECK(ST->online && !ST->cloud_online);
  app_state_handle(APP_EV_CLOUD_CHANGE, 1);
  CHECK(ST->online && ST->cloud_online);
  app_state_handle(APP_EV_NET_CHANGE, 0);
  CHECK(!ST->online && !ST->cloud_online);

  /* Hold REC in standby enters sync confirmation; short todo starts it. */
  EV(APP_EV_KEY_REC_LONG);
  CHECK(ST->overlay == APP_OV_SYNC_CONFIRM && sync_requests == 0);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->overlay == APP_OV_NONE && ST->sync == APP_SYNC_IDLE);
  EV(APP_EV_KEY_REC_LONG);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(ST->overlay == APP_OV_SYNC_PROGRESS && ST->sync == APP_SYNC_RUNNING &&
        sync_requests == 1);
  app_state_handle(APP_EV_BACKLOG, 123);
  CHECK(ST->backlog_s == 123 && last_render == APP_RENDER_STATUS_PARTIAL);
  app_state_handle(APP_EV_SYNC_CHANGE, APP_SYNC_DONE);
  CHECK(ST->overlay == APP_OV_SYNC_DONE && ST->sync == APP_SYNC_DONE);
  advance(2200);
  CHECK(ST->overlay == APP_OV_NONE && ST->sync == APP_SYNC_IDLE);
  sync_request_ok = false;
  EV(APP_EV_KEY_REC_LONG);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(ST->overlay == APP_OV_SYNC_FAILED && sync_requests == 2);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->overlay == APP_OV_NONE);
  sync_request_ok = true;

  /* Settings opens status, returns home, and its long press enters pairing. */
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 2);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 0);
  EV(APP_EV_KEY_BACK_LONG);
  CHECK(ST->mode == APP_MODE_PAIRING && pair_in == 1);
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_AP_READY);
  CHECK(ST->pairing == APP_PAIR_AP_READY);
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_WIFI_CONNECTING);
  CHECK(ST->pairing == APP_PAIR_WIFI_CONNECTING);
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_CLAIM_PENDING);
  CHECK(ST->pairing == APP_PAIR_CLAIM_PENDING);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->mode == APP_MODE_STANDBY && ST->pairing == APP_PAIR_IDLE &&
        pair_out == 1);

  /* An expired/revoked device token returns to the account-claim page
     without requiring WiFi credentials to be entered again. */
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_WIFI_CONNECTED);
  CHECK(ST->mode == APP_MODE_STANDBY);
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_CLAIM_PENDING);
  CHECK(ST->mode == APP_MODE_PAIRING && ST->pairing == APP_PAIR_CLAIM_PENDING);
  app_state_handle(APP_EV_PAIRING_CHANGE, APP_PAIR_BOUND);
  CHECK(ST->mode == APP_MODE_PAIRING && ST->pairing == APP_PAIR_BOUND &&
        pair_out == 2);
  advance(2500);
  CHECK(ST->mode == APP_MODE_STANDBY && ST->pairing == APP_PAIR_BOUND);

  /* A displayed 0% also leaves standalone voice-todo capture available. */
  app_state_handle(APP_EV_BATTERY, 0);
  EV(APP_EV_KEY_MARK_HOLD);
  CHECK(ST->overlay == APP_OV_TODO_LISTEN && todo_starts == 1 &&
        ST->battery == 0);
  advance(1200);
  CHECK(app_state_todo_elapsed_ms() == 1200);
  EV(APP_EV_RTC_ALARM);
  CHECK(ST->overlay == APP_OV_TODO_LISTEN && ST->reminder_pending);
  EV(APP_EV_KEY_MARK_RELEASE);
  CHECK(ST->overlay == APP_OV_TODO_OK && ST->todos == 1 && todo_ends == 1);
  advance(5000);
  CHECK(ST->overlay == APP_OV_REMINDER && !ST->reminder_pending);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->overlay == APP_OV_NONE);
  app_state_handle(APP_EV_BATTERY, 80);
  app_state_handle(APP_EV_TODO_RESULT, 1);
  CHECK(ST->overlay == APP_OV_TODO_CONFIRM);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(todo_actions == 1 && last_todo_confirm && ST->overlay == APP_OV_TODO_OK);
  app_state_handle(APP_EV_TODO_RESULT, 2);
  CHECK(ST->overlay == APP_OV_TODO_CREATED);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->overlay == APP_OV_NONE);

  /* Meeting recording keeps the status-page switch.  Short todo-key remains
     MARK.  Server V0.21.0 removed rolling minutes, so its recording-only long
     press is intentionally inert and must not redraw or leave captions. */
  CHECK(ST->scene == APP_SCENE_MEETING);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_RECORDING && ST->page == 0);
  CHECK(app_hold_ms('M') == 1500);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 1 && last_render == APP_RENDER_FAST);
  int renders_before_inert_hold = renders;
  EV(APP_EV_KEY_MARK_HOLD);
  EV(APP_EV_KEY_MARK_RELEASE);
  CHECK(ST->page == 1 && ST->overlay == APP_OV_NONE &&
        renders == renders_before_inert_hold);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(ST->page == 0 && last_render == APP_RENDER_FAST);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->paused);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(!ST->paused);
  EV(APP_EV_KEY_BACK_LONG);
  CHECK(ST->locked);
  EV(APP_EV_KEY_BACK_LONG);
  CHECK(!ST->locked);
  int marks_before_meeting = marks;
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(ST->favs == 0 && ST->marks == 1 && marks == marks_before_meeting + 1 &&
        ST->overlay == APP_OV_NONE);
  close_successfully();

  /* A result arriving during safe close cannot cover the close/ending page. */
  EV(APP_EV_KEY_REC_SHORT);
  EV(APP_EV_KEY_REC_LONG);
  CHECK(ST->mode == APP_MODE_CLOSING);
  app_state_handle(APP_EV_TODO_RESULT, 1);
  CHECK(ST->overlay == APP_OV_NONE && ST->todo_result_pending == 1);
  EV(APP_EV_SESSION_CLOSE_DONE);
  CHECK(ST->mode == APP_MODE_ENDING && ST->overlay == APP_OV_NONE);
  advance(3200);
  CHECK(ST->mode == APP_MODE_STANDBY && ST->overlay == APP_OV_TODO_CONFIRM);
  EV(APP_EV_KEY_BACK_SHORT);
  CHECK(todo_actions == 2 && !last_todo_confirm);
  advance(5000);

  app_state_set_reminder("学生会");
  EV(APP_EV_RTC_ALARM);
  EV(APP_EV_KEY_REC_SHORT);
  CHECK(ST->mode == APP_MODE_RECORDING);
  CHECK(strcmp(start_title, "学生会") == 0);
  EV(APP_EV_KEY_MARK_SHORT);
  CHECK(marks == marks_before_meeting + 2);
  close_successfully();

  /* A lost close-done queue event is recovered by polling durable storage. */
  close_status_result = APP_ERR_NONE;
  EV(APP_EV_KEY_REC_SHORT);
  EV(APP_EV_KEY_REC_LONG);
  CHECK(ST->mode == APP_MODE_CLOSING);
  advance(2000);
  CHECK(ST->mode == APP_MODE_ENDING && ST->storage_settled);
  advance(3200);
  CHECK(ST->mode == APP_MODE_STANDBY);

  /* A genuinely stuck writer stops blocking the UI forever.  A late durable
     close still recovers to the normal completion page. */
  close_status_result = APP_ERR_BUSY;
  EV(APP_EV_KEY_REC_SHORT);
  EV(APP_EV_KEY_REC_LONG);
  advance(16000);
  CHECK(ST->mode == APP_MODE_STORAGE_ERROR);
  CHECK(ST->error == APP_ERR_STORAGE_TIMEOUT && !ST->storage_settled);
  EV(APP_EV_SESSION_CLOSE_DONE);
  CHECK(ST->mode == APP_MODE_ENDING && ST->storage_settled);
  advance(3200);
  CHECK(ST->mode == APP_MODE_STANDBY);

  CHECK(app_hold_ms('R') == 1500);
  CHECK(app_hold_ms('M') == 600);
  CHECK(app_hold_ms('B') == 3000);
  printf(failures ? "%d state checks failed\n"
                  : "state checks passed (%d renders)\n",
         failures ? failures : renders);
  return failures ? 1 : 0;
}
