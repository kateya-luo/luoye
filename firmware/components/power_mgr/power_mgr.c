// power_mgr.c — I2C 器件最小驱动 + 状态轮询。
// 地址:MAX17048=0x36(VCELL 0x02, SOC 0x04),PCF8563=0x51,BQ25186=0x6A。
#include "power_mgr.h"
#include "power_soc.h"
#include "board_pins.h"
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_sleep.h"
#include "esp_log.h"

static const char *TAG = "power";
static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_max17048, s_pcf8563, s_bq25186;
static power_post_fn s_post;
static volatile bool s_alarm_armed;
static bool s_rtc_vl_logged;
static portMUX_TYPE s_diag_mux = portMUX_INITIALIZER_UNLOCKED;
static power_diag_sample_t s_diag;

enum {
  BQ_REG_STAT0 = 0x00,
  BQ_REG_STAT1 = 0x01,
  BQ_REG_ICHG_CTRL = 0x04,
  BQ_REG_IC_CTRL = 0x07,
  BQ_REG_TMR_ILIM = 0x08,
  BQ_REG_SYS_REG = 0x0A,
  BQ_REG_MASK_ID = 0x0C,
};

typedef struct {
  uint8_t stat0;
  uint8_t stat1;
  uint8_t ichg_ctrl;
  uint8_t tmr_ilim;
  uint8_t sys_reg;
  bool valid;
} bq25186_status_t;

// ---------- I2C 读写小工具 ----------
static esp_err_t rd(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *buf, size_t len) {
  return i2c_master_transmit_receive(dev, &reg, 1, buf, len, 100);
}
static esp_err_t wr(i2c_master_dev_handle_t dev, uint8_t reg, const uint8_t *buf, size_t len) {
  uint8_t tmp[8] = {reg};
  if (len > sizeof(tmp) - 1) return ESP_ERR_INVALID_ARG;
  memcpy(tmp + 1, buf, len);
  return i2c_master_transmit(dev, tmp, len + 1, 100);
}

// ---------- MAX17048 ----------
int power_battery_percent(void) {
  uint8_t b[2];
  if (rd(s_max17048, 0x04, b, 2) != ESP_OK) return -1;
  int soc = b[0];                       // 高字节=整数百分比(低字节 1/256)
  return soc > 100 ? 100 : soc;
}

static int power_battery_soc_x256(void) {
  uint8_t b[2];
  if (rd(s_max17048, 0x04, b, 2) != ESP_OK) return -1;
  uint16_t raw = ((uint16_t)b[0] << 8) | b[1];
  return raw > 100U * 256U ? 100 * 256 : (int)raw;
}
int power_battery_mv(void) {
  uint8_t b[2];
  if (rd(s_max17048, 0x02, b, 2) != ESP_OK) return -1;
  uint32_t raw = (b[0] << 8) | b[1];
  return (int)(raw * 78125ULL / 1000000ULL);   // 78.125 µV/LSB
}

// ---------- BQ25186 + PG 脚 ----------
static int bq_ichg_code_to_ma(uint8_t code) {
  code &= 0x7F;
  return code <= 30 ? code + 5 : 40 + (code - 31) * 10;
}

static uint8_t bq_ichg_ma_to_code(int ma) {
  if (ma <= 35) return (uint8_t)(ma < 5 ? 0 : ma - 5);
  if (ma > 1000) ma = 1000;
  return (uint8_t)(31 + (ma - 40 + 5) / 10);
}

static int bq_ilim_code_to_ma(uint8_t code) {
  static const uint16_t limits[] = {50, 100, 200, 300, 400, 500, 665, 1050};
  return limits[code & 0x07U];
}

static uint8_t bq_ilim_ma_to_code(int ma) {
  static const uint16_t limits[] = {50, 100, 200, 300, 400, 500, 665, 1050};
  for (uint8_t code = 0; code < 7; ++code) {
    if (ma <= limits[code]) return code;
  }
  return 7;
}

static bool bq_read_status(bq25186_status_t *status) {
  if (!status) return false;
  memset(status, 0, sizeof(*status));
  if (rd(s_bq25186, BQ_REG_STAT0, &status->stat0, 1) != ESP_OK ||
      rd(s_bq25186, BQ_REG_STAT1, &status->stat1, 1) != ESP_OK ||
      rd(s_bq25186, BQ_REG_ICHG_CTRL, &status->ichg_ctrl, 1) != ESP_OK ||
      rd(s_bq25186, BQ_REG_TMR_ILIM, &status->tmr_ilim, 1) != ESP_OK ||
      rd(s_bq25186, BQ_REG_SYS_REG, &status->sys_reg, 1) != ESP_OK) {
    return false;
  }
  status->valid = true;
  return true;
}

static esp_err_t bq25186_configure(void) {
  uint8_t id = 0, ichg_before = 0, ic_ctrl = 0, tmr_ilim = 0, sys_reg = 0;
  esp_err_t err = rd(s_bq25186, BQ_REG_MASK_ID, &id, 1);
  if (err != ESP_OK) return err;
  err = rd(s_bq25186, BQ_REG_ICHG_CTRL, &ichg_before, 1);
  if (err != ESP_OK) return err;

  /* Start conservatively until MAX17048 provides a valid SOC sample. */
  uint8_t ichg = bq_ichg_ma_to_code(BQ25186_BOOT_CHARGE_CURRENT_MA) & 0x7FU;
  err = wr(s_bq25186, BQ_REG_ICHG_CTRL, &ichg, 1);
  if (err != ESP_OK) return err;

  /* Start with the reset-default input clamp. The SOC policy raises it before
     requesting a higher battery charge current. */
  err = rd(s_bq25186, BQ_REG_TMR_ILIM, &tmr_ilim, 1);
  if (err != ESP_OK) return err;
  tmr_ilim = (uint8_t)((tmr_ilim & ~0x07U) |
                       bq_ilim_ma_to_code(BQ25186_BOOT_INPUT_LIMIT_MA));
  err = wr(s_bq25186, BQ_REG_TMR_ILIM, &tmr_ilim, 1);
  if (err != ESP_OK) return err;

  /* The downstream TPS63001 regulates the card rail, so BQ SYS does not need
     the reset-default fixed 4.5 V target. Battery tracking (VBAT + 225 mV,
     minimum 3.8 V) reduces linear loss and leaves DPPM headroom. Keep SYS_MODE
     and VDPPM protection unchanged. */
  err = rd(s_bq25186, BQ_REG_SYS_REG, &sys_reg, 1);
  if (err != ESP_OK) return err;
  sys_reg &= 0x1FU; /* SYS_REG_CTRL[7:5] = 000: battery tracking. */
  err = wr(s_bq25186, BQ_REG_SYS_REG, &sys_reg, 1);
  if (err != ESP_OK) return err;

  /* Reset default restores registers after 160 s. Disable that watchdog while
     preserving TS, recharge and safety-timer fields. */
  err = rd(s_bq25186, BQ_REG_IC_CTRL, &ic_ctrl, 1);
  if (err != ESP_OK) return err;
  ic_ctrl = (uint8_t)((ic_ctrl & ~0x03U) | 0x03U);
  err = wr(s_bq25186, BQ_REG_IC_CTRL, &ic_ctrl, 1);
  if (err != ESP_OK) return err;

  uint8_t verify_ichg = 0, verify_ctrl = 0, verify_ilim = 0, verify_sys = 0;
  err = rd(s_bq25186, BQ_REG_ICHG_CTRL, &verify_ichg, 1);
  if (err != ESP_OK || verify_ichg != ichg) return err == ESP_OK ? ESP_FAIL : err;
  err = rd(s_bq25186, BQ_REG_IC_CTRL, &verify_ctrl, 1);
  if (err != ESP_OK || (verify_ctrl & 0x03U) != 0x03U) {
    return err == ESP_OK ? ESP_FAIL : err;
  }
  err = rd(s_bq25186, BQ_REG_TMR_ILIM, &verify_ilim, 1);
  if (err != ESP_OK || (verify_ilim & 0x07U) != (tmr_ilim & 0x07U)) {
    return err == ESP_OK ? ESP_FAIL : err;
  }
  err = rd(s_bq25186, BQ_REG_SYS_REG, &verify_sys, 1);
  if (err != ESP_OK || (verify_sys & 0xE0U) != 0) {
    return err == ESP_OK ? ESP_FAIL : err;
  }
  ESP_LOGI(TAG,
           "LY|BQ|event=config id=0x%02X ichg_before=%dmA ichg=%dmA ilim=%dmA sys=tracking dppm=%s enabled=1 watchdog=off ce=%d",
           id, bq_ichg_code_to_ma(ichg_before),
           bq_ichg_code_to_ma(verify_ichg), bq_ilim_code_to_ma(verify_ilim),
           (verify_sys & 0x01U) ? "off" : "on",
           gpio_get_level(PIN_BQ_CE_N));
  return ESP_OK;
}

typedef struct {
  bool enabled;
  int charge_ma;
  int input_ma;
  const char *band;
} bq_charge_target_t;

static bq_charge_target_t bq_target_for_soc(int soc) {
  /* MAX17048 is a reporting fuel gauge, not the charge terminator.  Keep the
     1A CC setpoint and let BQ25186's CV/ITERM state machine declare done. */
  (void)soc;
  return (bq_charge_target_t){true, BQ25186_LOW_SOC_CHARGE_MA,
                             BQ25186_LOW_SOC_INPUT_MA, "1a_cc"};
}

static esp_err_t bq_apply_charge_policy(int soc,
                                        const bq25186_status_t *current) {
  if (!current || !current->valid || soc < 0 || soc > 100) {
    return ESP_ERR_INVALID_ARG;
  }
  bq_charge_target_t target = bq_target_for_soc(soc);
  uint8_t desired_ichg = bq_ichg_ma_to_code(target.charge_ma) & 0x7FU;
  if (!target.enabled) desired_ichg |= 0x80U;
  uint8_t desired_ilim = (uint8_t)((current->tmr_ilim & ~0x07U) |
                                   bq_ilim_ma_to_code(target.input_ma));
  bool ichg_changed = current->ichg_ctrl != desired_ichg;
  bool ilim_changed = current->tmr_ilim != desired_ilim;

  if (ichg_changed || ilim_changed) {
    bool current_enabled = (current->ichg_ctrl & 0x80U) == 0;
    int current_ma = bq_ichg_code_to_ma(current->ichg_ctrl);
    bool increasing = target.enabled &&
                      (!current_enabled || target.charge_ma > current_ma);
    esp_err_t err;
    if (increasing) {
      if (ilim_changed && (err = wr(s_bq25186, BQ_REG_TMR_ILIM,
                                    &desired_ilim, 1)) != ESP_OK) return err;
      if (ichg_changed && (err = wr(s_bq25186, BQ_REG_ICHG_CTRL,
                                    &desired_ichg, 1)) != ESP_OK) return err;
    } else {
      if (ichg_changed && (err = wr(s_bq25186, BQ_REG_ICHG_CTRL,
                                    &desired_ichg, 1)) != ESP_OK) return err;
      if (ilim_changed && (err = wr(s_bq25186, BQ_REG_TMR_ILIM,
                                    &desired_ilim, 1)) != ESP_OK) return err;
    }
  }

  uint8_t verify_ichg = 0, verify_ilim = 0;
  esp_err_t err = rd(s_bq25186, BQ_REG_ICHG_CTRL, &verify_ichg, 1);
  if (err != ESP_OK) return err;
  err = rd(s_bq25186, BQ_REG_TMR_ILIM, &verify_ilim, 1);
  if (err != ESP_OK) return err;
  if (verify_ichg != desired_ichg ||
      (verify_ilim & 0x07U) != (desired_ilim & 0x07U)) return ESP_FAIL;

  if (ichg_changed || ilim_changed) {
    ESP_LOGI(TAG,
             "LY|BQ_POLICY|event=apply soc=%d band=%s enabled=%d ichg=%dmA ilim=%dmA ce=%d",
             soc, target.band, target.enabled,
             bq_ichg_code_to_ma(verify_ichg),
             bq_ilim_code_to_ma(verify_ilim), gpio_get_level(PIN_BQ_CE_N));
  }
  return ESP_OK;
}

static esp_err_t bq25186_configure_with_retry(void) {
  esp_err_t err = ESP_FAIL;
  for (unsigned attempt = 1; attempt <= 3; ++attempt) {
    err = bq25186_configure();
    if (err == ESP_OK) return ESP_OK;
    ESP_LOGW(TAG, "LY|BQ|event=config_retry attempt=%u esp=%s",
             attempt, esp_err_to_name(err));
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  return err;
}

static app_charge_t charge_state_from_status(const bq25186_status_t *status) {
  if (gpio_get_level(PIN_BQ_PG_N) != 0) return APP_CHG_NONE;
  if (!status || !status->valid) return APP_CHG_NONE;
  bool disabled = (status->ichg_ctrl & 0x80U) != 0;
  bool ts_fault = (status->stat0 & 0x80U) != 0 ||
                  ((status->stat1 >> 3) & 0x03U) == 1;
  int chg = (status->stat0 >> 5) & 0x03;
  if (disabled || ts_fault) return APP_CHG_NONE;
  if (chg == 3) return APP_CHG_FULL;
  return APP_CHG_CHARGING;
}

static const char *bq_charge_phase(const bq25186_status_t *status) {
  if (!status || !status->valid) return "unreadable";
  if ((status->ichg_ctrl & 0x80U) != 0) return "disabled";
  if ((status->stat0 & 0x80U) != 0 ||
      ((status->stat1 >> 3) & 0x03U) == 1) return "ts_suspend";
  switch ((status->stat0 >> 5) & 0x03U) {
    case 1: return "cc";
    case 2: return "cv";
    case 3: return "done";
    default: return "idle";
  }
}

app_charge_t power_charge_state(void) {
  bq25186_status_t status;
  return charge_state_from_status(bq_read_status(&status) ? &status : NULL);
}

bool power_diag_snapshot(power_diag_sample_t *out) {
  if (!out) return false;
  portENTER_CRITICAL(&s_diag_mux);
  *out = s_diag;
  portEXIT_CRITICAL(&s_diag_mux);
  return out->sequence != 0;
}

// ---------- PCF8563 ----------
static uint8_t bcd(int v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }
static int unbcd(uint8_t v) { return ((v >> 4) & 0x0F) * 10 + (v & 0x0F); }

esp_err_t rtc_get_time(rtc_time_t *t) {
  uint8_t b[7];
  esp_err_t err = rd(s_pcf8563, 0x02, b, 7);
  if (err != ESP_OK) return err;
  if (b[0] & 0x80) {
    if (!s_rtc_vl_logged) {
      s_rtc_vl_logged = true;
      ESP_LOGW(TAG, "LY|RTC|event=invalid_clock reason=VL");
    }
    return ESP_ERR_INVALID_STATE;  // VL=1: oscillator value is invalid
  }
  s_rtc_vl_logged = false;
  t->sec = unbcd(b[0] & 0x7F); t->min = unbcd(b[1] & 0x7F); t->hour = unbcd(b[2] & 0x3F);
  t->day = unbcd(b[3] & 0x3F); t->wday = b[4] & 0x07;
  t->mon = unbcd(b[5] & 0x1F); t->year = 2000 + unbcd(b[6]);
  return ESP_OK;
}
esp_err_t rtc_set_time(const rtc_time_t *t) {
  uint8_t b[7] = {bcd(t->sec), bcd(t->min), bcd(t->hour), bcd(t->day),
                  (uint8_t)(t->wday & 7), bcd(t->mon), bcd(t->year % 100)};
  return wr(s_pcf8563, 0x02, b, 7);
}
esp_err_t rtc_set_alarm(int hour, int min) {
  uint8_t a[4] = {bcd(min), bcd(hour), 0x80, 0x80};   // 分+时参与匹配,日/星期屏蔽
  esp_err_t err = wr(s_pcf8563, 0x09, a, 4);
  if (err != ESP_OK) return err;
  uint8_t ctl2 = 0x02;                                // AIE=1,同时清 AF
  err = wr(s_pcf8563, 0x01, &ctl2, 1);
  if (err == ESP_OK) s_alarm_armed = true;
  return err;
}
esp_err_t rtc_set_alarm_utc(int64_t epoch_utc) {
  if (epoch_utc <= 0) return ESP_ERR_INVALID_ARG;
  time_t value = (time_t)epoch_utc;
  struct tm utc;
  if (!gmtime_r(&value, &utc)) return ESP_ERR_INVALID_ARG;
  uint8_t a[4] = {bcd(utc.tm_min), bcd(utc.tm_hour),
                  bcd(utc.tm_mday), 0x80};
  esp_err_t err = wr(s_pcf8563, 0x09, a, 4);
  if (err != ESP_OK) return err;
  uint8_t ctl2 = 0x02;
  err = wr(s_pcf8563, 0x01, &ctl2, 1);
  if (err == ESP_OK) s_alarm_armed = true;
  return err;
}
esp_err_t rtc_snooze_minutes(int minutes) {
  if (minutes <= 0) return ESP_ERR_INVALID_ARG;
  return rtc_set_alarm_utc((int64_t)time(NULL) + minutes * 60LL);
}
esp_err_t rtc_clear_alarm(void) {
  uint8_t ctl2 = 0x00;                                // AIE=0, AF=0
  esp_err_t err = wr(s_pcf8563, 0x01, &ctl2, 1);
  if (err == ESP_OK) s_alarm_armed = false;
  return err;
}

esp_err_t rtc_sync_from_system(void) {
  time_t now = time(NULL);
  struct tm utc;
  if (now < 1577836800 || !gmtime_r(&now, &utc)) return ESP_ERR_INVALID_STATE;
  rtc_time_t value = {
    .year = utc.tm_year + 1900, .mon = utc.tm_mon + 1, .day = utc.tm_mday,
    .hour = utc.tm_hour, .min = utc.tm_min, .sec = utc.tm_sec,
    .wday = utc.tm_wday,
  };
  rtc_time_t current;
  if (rtc_get_time(&current) == ESP_OK) {
    struct tm current_utc = {
      .tm_year = current.year - 1900, .tm_mon = current.mon - 1,
      .tm_mday = current.day, .tm_hour = current.hour,
      .tm_min = current.min, .tm_sec = current.sec, .tm_isdst = 0,
    };
    time_t rtc_epoch = mktime(&current_utc); /* TZ is UTC0 in app_main. */
    int64_t delta = (int64_t)now - (int64_t)rtc_epoch;
    if (delta >= -2 && delta <= 2) {
      ESP_LOGI(TAG, "LY|RTC|event=sync_skip delta_s=%lld", (long long)delta);
      return ESP_OK;
    }
    ESP_LOGI(TAG, "LY|RTC|event=sync_write delta_s=%lld", (long long)delta);
  } else {
    ESP_LOGI(TAG, "LY|RTC|event=sync_write reason=invalid_or_unset");
  }
  return rtc_set_time(&value);
}

esp_err_t rtc_restore_system(void) {
  rtc_time_t value;
  esp_err_t err = rtc_get_time(&value);
  if (err != ESP_OK || value.year < 2020 || value.year > 2099 ||
      value.mon < 1 || value.mon > 12 || value.day < 1 || value.day > 31 ||
      value.hour < 0 || value.hour > 23 || value.min < 0 || value.min > 59 ||
      value.sec < 0 || value.sec > 59) {
    return err == ESP_OK ? ESP_ERR_INVALID_STATE : err;
  }
  struct tm utc = {
    .tm_year = value.year - 1900, .tm_mon = value.mon - 1,
    .tm_mday = value.day, .tm_hour = value.hour,
    .tm_min = value.min, .tm_sec = value.sec, .tm_isdst = 0,
  };
  time_t epoch = mktime(&utc); /* app_main fixes TZ=UTC0 before init. */
  if (epoch < 1577836800) return ESP_ERR_INVALID_STATE;
  struct timeval tv = {.tv_sec = epoch, .tv_usec = 0};
  return settimeofday(&tv, NULL) == 0 ? ESP_OK : ESP_FAIL;
}

bool rtc_alarm_armed(void) { return s_alarm_armed; }

// ---------- 功耗 ----------
void power_set_low_noise(bool on) { gpio_set_level(PIN_PWR_MODE, on); }

void power_enter_off(void) {
  // GPIO41 is not RTC-IO, so an armed PCF8563 reminder uses light sleep.
  // With no reminder we retain deep sleep and three RTC-IO key wake sources.
  if (s_alarm_armed) {
    const int wake_pins[] = {PIN_KEY_REC, PIN_RTC_INT};
    ESP_LOGI(TAG, "进入提醒待机(light sleep,RTC 或长按 REC 唤醒)");
    for (;;) {
      for (size_t i = 0; i < sizeof(wake_pins) / sizeof(wake_pins[0]); i++) {
        gpio_wakeup_enable(wake_pins[i], GPIO_INTR_LOW_LEVEL);
      }
      esp_sleep_enable_gpio_wakeup();
      esp_light_sleep_start();
      ESP_LOGI(TAG, "LY|WAKE|mode=light cause=%d rtc_int=%d rec=%d",
               (int)esp_sleep_get_wakeup_cause(), gpio_get_level(PIN_RTC_INT),
               gpio_get_level(PIN_KEY_REC));
      for (size_t i = 0; i < sizeof(wake_pins) / sizeof(wake_pins[0]); i++) {
        gpio_wakeup_disable(wake_pins[i]);
      }
      if (gpio_get_level(PIN_RTC_INT) == 0) return;
      bool held = gpio_get_level(PIN_KEY_REC) == 0;
      for (int i = 0; held && i < 300; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
        held = gpio_get_level(PIN_KEY_REC) == 0;
      }
      if (held) return;
    }
  }
  // ⚠ 按键无外部上拉(仅 100nF),深睡里数字域上拉失效 → 必须开 RTC 域上拉,否则引脚
  //   悬空会随机误唤醒。GPIO1/2/4 都是 RTC-IO,支持 RTC 上拉。
  const int keys[] = {PIN_KEY_REC, PIN_KEY_MARK, PIN_KEY_BACK};
  for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
    rtc_gpio_init(keys[i]);
    rtc_gpio_set_direction(keys[i], RTC_GPIO_MODE_INPUT_ONLY);
    rtc_gpio_pullup_en(keys[i]);
    rtc_gpio_pulldown_dis(keys[i]);
  }
  const uint64_t mask = (1ULL << PIN_KEY_REC) | (1ULL << PIN_KEY_MARK) | (1ULL << PIN_KEY_BACK);
  esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  ESP_LOGI(TAG, "进入深睡(按键唤醒)");
  esp_deep_sleep_start();
}

// ---------- 轮询任务:电量/充电/RTC 闹钟标志 → 状态机事件 ----------
static void poll_task(void *arg) {
  (void)arg;
  app_charge_t last_chg = (app_charge_t)-1;
  app_charge_t candidate_chg = (app_charge_t)-1;
  unsigned candidate_count = 0;
  unsigned bq_log_divider = 0;
  int history[3] = {-1, -1, -1};
  unsigned history_count = 0, history_index = 0;
  bool used_voltage_last = false;
  int reported_soc = -1;
  TickType_t reported_step_tick = 0;
  for (;;) {
    int gauge_soc_x256 = power_battery_soc_x256();
    int gauge_soc = gauge_soc_x256 < 0 ? -1 : gauge_soc_x256 / 256;
    int soc = gauge_soc;
    int control_soc = gauge_soc;
    int mv = power_battery_mv();
    bool used_voltage = soc < 0;
    if (used_voltage && mv > 0) {
      if (mv >= 4200) soc = 100;
      else if (mv >= 4100) soc = 90;
      else if (mv >= 4000) soc = 80;
      else if (mv >= 3900) soc = 65;
      else if (mv >= 3800) soc = 45;
      else if (mv >= 3700) soc = 25;
      else if (mv >= 3600) soc = 12;
      else if (mv >= 3500) soc = 6;
      else if (mv >= 3400) soc = 3;
      else soc = 1;
    }
    int calibrated_soc = soc;
    if (!used_voltage && gauge_soc_x256 >= 0) {
      calibrated_soc = power_soc_calibrate_x256(gauge_soc_x256);
    }
    int filtered_soc = calibrated_soc;
    if (calibrated_soc >= 0) {
      history[history_index] = calibrated_soc;
      history_index = (history_index + 1U) % 3U;
      if (history_count < 3) history_count++;
      if (history_count == 3 && calibrated_soc > 3 && mv > 3400) {
        int a = history[0], b = history[1], c = history[2];
        filtered_soc = a > b ? (b > c ? b : (a > c ? c : a))
                             : (a > c ? a : (b > c ? c : b));
      }
      if (used_voltage != used_voltage_last) {
        ESP_LOGW(TAG, "LY|POWER|battery_source=%s soc=%d mv=%d",
                 used_voltage ? "voltage_fallback" : "max17048",
                 filtered_soc, mv);
      }
      used_voltage_last = used_voltage;
    } else {
      ESP_LOGW(TAG, "LY|POWER|event=battery_read_failed soc=%d mv=%d", soc, mv);
    }
    bq25186_status_t bq = {0};
    bool bq_ok = bq_read_status(&bq);
    bool unplugged = gpio_get_level(PIN_BQ_PG_N) != 0;
    if (!unplugged && bq_ok && control_soc >= 0) {
      esp_err_t policy_error = bq_apply_charge_policy(control_soc, &bq);
      if (policy_error == ESP_OK) {
        /* Refresh the status used by the UI after a register change. */
        bq_ok = bq_read_status(&bq);
      } else {
        ESP_LOGW(TAG, "LY|BQ_POLICY|event=apply_failed soc=%d esp=%s keep_previous=1",
                 control_soc, esp_err_to_name(policy_error));
      }
    }
    app_charge_t chg = unplugged ? APP_CHG_NONE :
                       charge_state_from_status(bq_ok ? &bq : NULL);
    /* VIN present 时的单次 I2C 失败不构成状态变化，避免墨水屏来回刷新。 */
    bool have_charge_sample = unplugged || bq_ok;
    if (have_charge_sample) {
      if (chg != candidate_chg) {
        candidate_chg = chg;
        candidate_count = 1;
      } else if (candidate_count < 2) {
        candidate_count++;
      }
    }
    /* 插拔 USB 立即生效；充电/充满切换需要连续两次 5 秒采样一致。 */
    if (have_charge_sample &&
        (last_chg == (app_charge_t)-1 || unplugged || candidate_count >= 2)) {
      if (candidate_chg != last_chg) {
        last_chg = candidate_chg;
        s_post(APP_EV_CHARGE_CHANGE, candidate_chg);
      }
    }
    app_charge_t stable_chg = last_chg == (app_charge_t)-1 ? chg : last_chg;
    int displayed_soc = calibrated_soc;
    if (calibrated_soc >= 0) {
      int target_soc = stable_chg == APP_CHG_FULL ? 100 : filtered_soc;
      if (!unplugged && stable_chg != APP_CHG_FULL && target_soc >= 100) {
        target_soc = 99;
      }
      TickType_t now_tick = xTaskGetTickCount();
      if (reported_soc < 0) {
        reported_soc = target_soc;
        reported_step_tick = now_tick;
      } else if (stable_chg == APP_CHG_FULL) {
        reported_soc = 100;
        reported_step_tick = now_tick;
      } else if (!unplugged) {
        if (target_soc > reported_soc &&
            now_tick - reported_step_tick >= pdMS_TO_TICKS(30 * 1000)) {
          reported_soc++;
          reported_step_tick = now_tick;
        }
      } else if (target_soc < reported_soc) {
        if (target_soc <= 10 || mv <= 3550) {
          reported_soc = target_soc;
          reported_step_tick = now_tick;
        } else if (now_tick - reported_step_tick >= pdMS_TO_TICKS(60 * 1000)) {
          reported_soc--;
          reported_step_tick = now_tick;
        }
      }
      displayed_soc = reported_soc;
      s_post(APP_EV_BATTERY, displayed_soc);
    }

    power_diag_sample_t diag = {
      .gauge_soc_x256 = gauge_soc_x256,
      .calibrated_soc = calibrated_soc,
      .filtered_soc = filtered_soc,
      .displayed_soc = displayed_soc,
      .battery_mv = mv,
      .charge_ma = bq_ok ? bq_ichg_code_to_ma(bq.ichg_ctrl) : -1,
      .input_limit_ma = bq_ok ? bq_ilim_code_to_ma(bq.tmr_ilim) : -1,
      .charge_state = stable_chg,
      .bq_stat0 = bq.stat0,
      .bq_stat1 = bq.stat1,
      .bq_ichg_ctrl = bq.ichg_ctrl,
      .bq_tmr_ilim = bq.tmr_ilim,
      .bq_sys_reg = bq.sys_reg,
      .gauge_ok = gauge_soc_x256 >= 0,
      .bq_ok = bq_ok,
      .usb_present = !unplugged,
      .voltage_fallback = used_voltage,
    };
    (void)rd(s_max17048, 0x0C, diag.max_config, 2);
    (void)rd(s_max17048, 0x0A, diag.max_hibrt, 2);
    (void)rd(s_max17048, 0x1A, diag.max_status, 2);
    (void)rd(s_max17048, 0x08, diag.max_version, 2);
    portENTER_CRITICAL(&s_diag_mux);
    diag.sequence = s_diag.sequence + 1U;
    s_diag = diag;
    portEXIT_CRITICAL(&s_diag_mux);
    if (bq_log_divider++ % 12U == 0U || !bq_ok) {
      ESP_LOGI(TAG,
               "LY|BQ|pg=%d ce=%d ok=%d phase=%s stat0=0x%02X stat1=0x%02X ui_chg=%d ichg=%dmA ilim=%dmA sys_track=%d dppm_en=%d ilim_active=%d vdppm=%d vindpm=%d therm=%d soc=%d mv=%d",
               gpio_get_level(PIN_BQ_PG_N), gpio_get_level(PIN_BQ_CE_N), bq_ok,
               bq_charge_phase(bq_ok ? &bq : NULL), bq.stat0, bq.stat1,
               (int)(last_chg == (app_charge_t)-1 ? chg : last_chg),
               bq_ok ? bq_ichg_code_to_ma(bq.ichg_ctrl) : -1,
               bq_ok ? bq_ilim_code_to_ma(bq.tmr_ilim) : -1,
               bq_ok ? (bq.sys_reg & 0xE0U) == 0 : -1,
               bq_ok ? (bq.sys_reg & 0x01U) == 0 : -1,
               bq_ok ? !!(bq.stat0 & 0x10U) : -1,
               bq_ok ? !!(bq.stat0 & 0x08U) : -1,
               bq_ok ? !!(bq.stat0 & 0x04U) : -1,
               bq_ok ? !!(bq.stat0 & 0x02U) : -1, displayed_soc, mv);
      ESP_LOGI(TAG,
               "LY|POWER_DIAG|source=power seq=%lu raw=%d.%02d calibrated=%d filtered=%d shown=%d mv=%d usb=%d charge=%d phase=%u ichg=%d ilim=%d config=0x%02X%02X hibrt=0x%02X%02X status=0x%02X%02X version=0x%02X%02X fallback=%d",
               (unsigned long)diag.sequence,
               gauge_soc_x256 < 0 ? -1 : gauge_soc_x256 / 256,
               gauge_soc_x256 < 0 ? 0 : (gauge_soc_x256 % 256) * 100 / 256,
               calibrated_soc, filtered_soc, displayed_soc, mv, !unplugged,
               (int)stable_chg,
               (unsigned)((bq.stat0 >> 5) & 0x03U), diag.charge_ma,
               diag.input_limit_ma, diag.max_config[0], diag.max_config[1],
               diag.max_hibrt[0], diag.max_hibrt[1], diag.max_status[0],
               diag.max_status[1], diag.max_version[0], diag.max_version[1],
               used_voltage);
    }
    // RTC 闹钟:INT 低电平 或 AF 标志置位(轮询兜底,防中断边沿丢失)
    uint8_t ctl2 = 0;
    if (gpio_get_level(PIN_RTC_INT) == 0 ||
        (rd(s_pcf8563, 0x01, &ctl2, 1) == ESP_OK && (ctl2 & 0x08))) {
      rtc_clear_alarm();
      s_post(APP_EV_RTC_ALARM, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(5000));
  }
}

esp_err_t power_mgr_init(power_post_fn post) {
  s_post = post;
  gpio_config_t in = {
    .pin_bit_mask = (1ULL << PIN_BQ_INT_N) | (1ULL << PIN_BQ_PG_N) |
                    (1ULL << PIN_BAT_ALRT) | (1ULL << PIN_RTC_INT),
    .mode = GPIO_MODE_INPUT,
    .pull_up_en = GPIO_PULLUP_ENABLE,   // 开漏中断脚
  };
  esp_err_t err = gpio_config(&in);
  if (err != ESP_OK) return err;
  gpio_config_t out = {
    .pin_bit_mask = (1ULL << PIN_PWR_MODE),
    .mode = GPIO_MODE_OUTPUT,
  };
  err = gpio_config(&out);
  if (err != ESP_OK) return err;
  gpio_config_t charge_enable = {
    .pin_bit_mask = (1ULL << PIN_BQ_CE_N),
    .mode = GPIO_MODE_OUTPUT_OD,
    .pull_up_en = GPIO_PULLUP_DISABLE,
    .pull_down_en = GPIO_PULLDOWN_DISABLE,
  };
  err = gpio_config(&charge_enable);
  if (err != ESP_OK) return err;
  gpio_set_level(PIN_BQ_CE_N, 0);
  // CE_N 低有效：板上 10k 下拉兜底，固件也明确开漏拉低。

  i2c_master_bus_config_t bus = {
    .i2c_port = -1,
    .sda_io_num = PIN_I2C_SDA, .scl_io_num = PIN_I2C_SCL,
    .clk_source = I2C_CLK_SRC_DEFAULT,
    .glitch_ignore_cnt = 7,
    // 板上已有 10k 上拉,内部上拉不开
  };
  err = i2c_new_master_bus(&bus, &s_bus);
  if (err != ESP_OK) return err;
  i2c_device_config_t dev = {.scl_speed_hz = 100000};
  dev.device_address = I2C_ADDR_MAX17048;
  err = i2c_master_bus_add_device(s_bus, &dev, &s_max17048);
  if (err != ESP_OK) return err;
  dev.device_address = I2C_ADDR_PCF8563;
  err = i2c_master_bus_add_device(s_bus, &dev, &s_pcf8563);
  if (err != ESP_OK) return err;
  dev.device_address = I2C_ADDR_BQ25186;
  err = i2c_master_bus_add_device(s_bus, &dev, &s_bq25186);
  if (err != ESP_OK) return err;

  err = bq25186_configure_with_retry();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "LY|BQ|event=config_failed esp=%s", esp_err_to_name(err));
    return err;
  }

  if (xTaskCreate(poll_task, "power_poll", 3072, NULL, 4, NULL) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}
