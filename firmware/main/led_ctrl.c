// led_ctrl.c — 50ms tick 的花样发生器,直接读状态机快照决定三灯输出。
// REC 红:录音常亮 / 暂停慢闪(1.1s) / 收尾快闪(0.3s) / 标记双闪(优先级最高)
// FULL 黄:提醒页慢闪 / SD将满常亮 / (录音中离线 或 积压≥30s)慢闪
// CHG 绿:充电中常亮
#include "led_ctrl.h"
#include "app_state.h"
#include "board_pins.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_timer.h"

static volatile int64_t s_mark_flash_until;   // 双闪截止时刻(ms)
static volatile int64_t s_self_test_start;    // 自检起始时刻,0=非自检

static inline int64_t now_ms(void) { return esp_timer_get_time() / 1000; }
static inline bool blink(int64_t t, int period_ms) { return (t / (period_ms / 2)) % 2 == 0; }

void led_ctrl_mark_flash(void) { s_mark_flash_until = now_ms() + 760; }
void led_ctrl_self_test(void)  { s_self_test_start = now_ms(); }

static void led_task(void *arg) {
  (void)arg;
  for (;;) {
    const app_state_t *s = app_state_get();
    int64_t t = now_ms();
    bool r = false, f = false, c = false;

    if (s_self_test_start && t - s_self_test_start < 1440) {   // 三灯轮流点亮 3×360ms + 熄灭
      int step = (int)((t - s_self_test_start) / 360);
      r = step == 0; f = step == 1; c = step == 2;
    } else {
      s_self_test_start = 0;
      // REC 红
      if (t < s_mark_flash_until) r = blink(t, 240);                       // 双闪
      else if (s->mode == APP_MODE_STARTING || s->mode == APP_MODE_RECORDING)
        r = s->paused ? blink(t, 1100) : true;
      else if (s->mode == APP_MODE_CLOSING) r = blink(t, 300);
      else if (s->mode == APP_MODE_STORAGE_ERROR) {
        r = blink(t, 400);
        f = !r;
      }
      // FULL 黄
      if (s->overlay == APP_OV_REMINDER) f = blink(t, 1100);
      else if (s->mode == APP_MODE_STORAGE_ERROR) { /* pattern set above */ }
      else if (s->sd_low) f = true;
      else if ((s->mode == APP_MODE_RECORDING && !s->online) || s->backlog_s >= 30) f = blink(t, 1100);
      // CHG 绿
      c = s->charging == APP_CHG_CHARGING;
    }
    gpio_set_level(PIN_LED_REC, r);
    gpio_set_level(PIN_LED_FULL, f);
    gpio_set_level(PIN_LED_CHG, c);
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

esp_err_t led_ctrl_init(void) {
  gpio_config_t cfg = {
    .pin_bit_mask = (1ULL << PIN_LED_REC) | (1ULL << PIN_LED_FULL) | (1ULL << PIN_LED_CHG),
    .mode = GPIO_MODE_OUTPUT,
  };
  esp_err_t err = gpio_config(&cfg);
  if (err != ESP_OK) return err;
  if (xTaskCreate(led_task, "leds", 2560, NULL, 5, NULL) != pdPASS) return ESP_ERR_NO_MEM;
  return ESP_OK;
}
