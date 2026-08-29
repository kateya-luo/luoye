// power_mgr.h — 电池/充电/RTC 三件套(BQ25186 + MAX17048 + PCF8563,共享 I2C)
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "app_state.h"

typedef void (*power_post_fn)(app_event_t ev, int32_t arg);

/* Latest locally sampled fuel-gauge/charger state.  This snapshot is consumed
 * by the SD diagnostics task and never depends on Wi-Fi or the server. */
typedef struct {
  uint32_t sequence;
  int32_t gauge_soc_x256;
  int16_t mapped_soc;
  int16_t filtered_soc;
  int16_t displayed_soc;
  int16_t battery_mv;
  int16_t charge_ma;
  int16_t input_limit_ma;
  app_charge_t charge_state;
  uint8_t max_config[2];
  uint8_t max_hibrt[2];
  uint8_t max_status[2];
  uint8_t max_version[2];
  uint8_t bq_stat0;
  uint8_t bq_stat1;
  uint8_t bq_ichg_ctrl;
  uint8_t bq_tmr_ilim;
  uint8_t bq_sys_reg;
  bool gauge_ok;
  bool bq_ok;
  bool usb_present;
  bool voltage_fallback;
} power_diag_sample_t;

esp_err_t power_mgr_init(power_post_fn post);   // 建 I2C 总线 + 中断脚 + 30s 轮询任务

// —— 即时查询 ——
int  power_battery_percent(void);     // MAX17048 SOC,-1=读取失败
int  power_battery_mv(void);          // 电池电压 mV
app_charge_t power_charge_state(void);// 由 BQ_PG_N + BQ25186 状态寄存器合成
bool power_diag_snapshot(power_diag_sample_t *out);

// —— RTC(PCF8563,掉电由纽扣/主电池 VBAT+ 域维持) ——
typedef struct { int year, mon, day, hour, min, sec, wday; } rtc_time_t;
esp_err_t rtc_get_time(rtc_time_t *t);
esp_err_t rtc_set_time(const rtc_time_t *t);
esp_err_t rtc_set_alarm(int hour, int min);     // 当日闹钟;INT 低有效 → PIN_RTC_INT
esp_err_t rtc_set_alarm_utc(int64_t epoch_utc); // 最近一条提醒:分/时/日精确匹配
esp_err_t rtc_snooze_minutes(int minutes);      // 在当前时刻基础上 +N 分钟重设闹钟
esp_err_t rtc_clear_alarm(void);                // 清 AF 标志 + 关闹钟
esp_err_t rtc_sync_from_system(void);           // SNTP UTC → PCF8563
esp_err_t rtc_restore_system(void);             // PCF8563 UTC → 系统时间
bool rtc_alarm_armed(void);

// —— 功耗档位 ——
void power_set_low_noise(bool on);    // TPS63001 PS/SYNC:录音时 true(强制PWM)
void power_enter_off(void);           // 有提醒用 light sleep(GPIO41),否则深睡
