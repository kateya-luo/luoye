// input_keys.c — 10ms 轮询去抖。移植模拟器 bind() 语义:
//   按下时锁定该键当前态的长按阈值(holdMs);
//   达到阈值 → 触发长按事件(MARK 待机态为语音待办,松开再发 RELEASE);
//   未达阈值就松开 → 短按事件。
#include "input_keys.h"
#include "board_pins.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_timer.h"

#define POLL_MS      10
#define DEBOUNCE_MS  30

typedef struct {
  int pin;
  char name;                    // 'R' / 'M' / 'B'
  app_event_t ev_short, ev_long, ev_release;
  bool has_release;
  // 运行态
  bool stable_down, raw_down;
  int64_t raw_since_ms, press_ms;
  uint32_t hold_threshold;
  bool long_fired;
} key_t;

static key_t s_keys[] = {
  {
    .pin = PIN_KEY_REC, .name = 'R',
    .ev_short = APP_EV_KEY_REC_SHORT, .ev_long = APP_EV_KEY_REC_LONG,
  },
  {
    .pin = PIN_KEY_MARK, .name = 'M',
    .ev_short = APP_EV_KEY_MARK_SHORT, .ev_long = APP_EV_KEY_MARK_HOLD,
    .ev_release = APP_EV_KEY_MARK_RELEASE, .has_release = true,
  },
  {
    .pin = PIN_KEY_BACK, .name = 'B',
    .ev_short = APP_EV_KEY_BACK_SHORT, .ev_long = APP_EV_KEY_BACK_LONG,
  },
};
static key_post_fn s_post;

static void key_task(void *arg) {
  (void)arg;
  for (;;) {
    int64_t now = esp_timer_get_time() / 1000;
    for (size_t i = 0; i < sizeof(s_keys) / sizeof(s_keys[0]); i++) {
      key_t *k = &s_keys[i];
      bool down = gpio_get_level(k->pin) == 0;   // 按下拉低
      if (down != k->raw_down) { k->raw_down = down; k->raw_since_ms = now; }
      if (down == k->stable_down || now - k->raw_since_ms < DEBOUNCE_MS) {
        // 已按稳:检查是否达到长按阈值
        if (k->stable_down && !k->long_fired && now - k->press_ms >= k->hold_threshold) {
          k->long_fired = true;
          s_post(k->ev_long, 0);
        }
        continue;
      }
      k->stable_down = down;
      if (down) {                                // 按下沿:锁定当前态阈值
        k->press_ms = now;
        k->hold_threshold = app_hold_ms(k->name);
        k->long_fired = false;
      } else {                                   // 松开沿
        if (k->long_fired) {
          if (k->has_release) s_post(k->ev_release, 0);
        } else {
          s_post(k->ev_short, 0);
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(POLL_MS));
  }
}

esp_err_t input_keys_init(key_post_fn post) {
  s_post = post;
  uint64_t mask = 0;
  for (size_t i = 0; i < sizeof(s_keys) / sizeof(s_keys[0]); i++) mask |= 1ULL << s_keys[i].pin;
  gpio_config_t cfg = {
    .pin_bit_mask = mask,
    .mode = GPIO_MODE_INPUT,
    .pull_up_en = GPIO_PULLUP_ENABLE,     // 板上无外部上拉,必须内部上拉
    .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE,
  };
  esp_err_t err = gpio_config(&cfg);
  if (err != ESP_OK) return err;
  if (xTaskCreate(key_task, "keys", 3072, NULL, 10, NULL) != pdPASS) return ESP_ERR_NO_MEM;
  return ESP_OK;
}
