// audio_pdm.c — ESP-IDF v5 i2s_pdm 新驱动。左右麦均分混合成单声道。
#include "audio_pdm.h"
#include "board_pins.h"
#include "dualmic_frontend.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "driver/i2s_pdm.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "audio";
static i2s_chan_handle_t s_rx;
static StreamBufferHandle_t s_ring;
static volatile bool s_running, s_muted;
static TaskHandle_t s_task;
static portMUX_TYPE s_levels_lock = portMUX_INITIALIZER_UNLOCKED;
static audio_pdm_levels_t s_levels;
static portMUX_TYPE s_stats_lock = portMUX_INITIALIZER_UNLOCKED;
static audio_pdm_stream_stats_t s_stats;
static portMUX_TYPE s_tap_lock = portMUX_INITIALIZER_UNLOCKED;
static audio_pdm_stereo_tap_t s_stereo_tap;
static void *s_stereo_tap_ctx;
static audio_pdm_mono_tap_t s_mono_tap;
static void *s_mono_tap_ctx;
static bool s_frontend_cal_logged;

#define CHUNK_SAMPLES DMFE_FRAME_SAMPLES  // 20 ms frontend frame at 16 kHz
#define PDM_CLOCK_HZ (AUDIO_SAMPLE_RATE * 128U)
#define MIC_STARTUP_MS 80

static inline int16_t apply_pcm_gain(int16_t sample) {
  int32_t amplified = (int32_t)sample * (int32_t)AUDIO_PDM_PCM_GAIN;
  if (amplified > INT16_MAX) return INT16_MAX;
  if (amplified < INT16_MIN) return INT16_MIN;
  return (int16_t)amplified;
}

static void capture_task(void *arg) {
  (void)arg;
  static int16_t stereo[CHUNK_SAMPLES * 2];
  static int16_t mono[CHUNK_SAMPLES];
  for (;;) {
    if (!s_running) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }
    size_t got = 0;
    if (i2s_channel_read(s_rx, stereo, sizeof(stereo), &got, 1000) != ESP_OK || got == 0) continue;
    bool muted = s_muted;
    size_t frames = got / (2 * sizeof(int16_t));
    bool enhanced = false;
    if (!muted) {
      enhanced = dmfe_process_right_left(stereo, frames, mono);
      if (!enhanced) {
        for (size_t i = 0; i < frames; ++i) {
          int32_t right = apply_pcm_gain(stereo[2 * i]);
          int32_t left = apply_pcm_gain(stereo[2 * i + 1]);
          mono[i] = (int16_t)((left + right) / 2);
        }
      }
      audio_pdm_mono_tap_t mono_tap;
      void *mono_tap_ctx;
      portENTER_CRITICAL(&s_tap_lock);
      mono_tap = s_mono_tap;
      mono_tap_ctx = s_mono_tap_ctx;
      portEXIT_CRITICAL(&s_tap_lock);
      if (mono_tap) mono_tap(mono, frames, mono_tap_ctx);

      dmfe_realtime_stats_t frontend;
      dmfe_get_stats(&frontend);
      if (!s_frontend_cal_logged && frontend.calibrated) {
        s_frontend_cal_logged = true;
        ESP_LOGI(TAG,
                 "LY|AUDIO_FRONTEND|state=calibrated profile=dualmic-r3 balance=%.3f/%.3f noise=%.1f delay=%.2f corr=%.3f",
                 (double)frontend.balance_gain_ch1,
                 (double)frontend.balance_gain_ch2,
                 (double)frontend.calibration_noise_rms,
                 (double)frontend.delay_samples,
                 (double)frontend.correlation);
      }
    }
    for (size_t i = 0; i < frames * 2; i++) {
      stereo[i] = apply_pcm_gain(stereo[i]);
    }
    audio_pdm_stereo_tap_t tap;
    void *tap_ctx;
    portENTER_CRITICAL(&s_tap_lock);
    tap = s_stereo_tap;
    tap_ctx = s_stereo_tap_ctx;
    portEXIT_CRITICAL(&s_tap_lock);
    if (tap) tap(stereo, frames, tap_ctx);
    if (muted) continue; /* Main WAV pauses; the explicit todo tap still records. */

    uint64_t sum_l = 0, sum_r = 0;
    uint16_t peak_l = 0, peak_r = 0;
    int16_t min_l = INT16_MAX, max_l = INT16_MIN;
    int16_t min_r = INT16_MAX, max_r = INT16_MIN;
    size_t equal_count = 0, clipped_l = 0, clipped_r = 0;
    for (size_t i = 0; i < frames; i++) {
      // ESP32-S3 PDM RX 的立体声 DMA 顺序固定为 right, left。
      int32_t right = stereo[2 * i], left = stereo[2 * i + 1];
      uint16_t abs_l = (uint16_t)(left < 0 ? -left : left);
      uint16_t abs_r = (uint16_t)(right < 0 ? -right : right);
      sum_l += abs_l; sum_r += abs_r;
      if (abs_l > peak_l) peak_l = abs_l;
      if (abs_r > peak_r) peak_r = abs_r;
      if (left < min_l) min_l = (int16_t)left;
      if (left > max_l) max_l = (int16_t)left;
      if (right < min_r) min_r = (int16_t)right;
      if (right > max_r) max_r = (int16_t)right;
      if (left == right) equal_count++;
      if (abs_l >= 30000) clipped_l++;
      if (abs_r >= 30000) clipped_r++;
    }
    portENTER_CRITICAL(&s_levels_lock);
    s_levels.mean_abs_left = frames ? (uint16_t)(sum_l / frames) : 0;
    s_levels.mean_abs_right = frames ? (uint16_t)(sum_r / frames) : 0;
    s_levels.peak_left = peak_l;
    s_levels.peak_right = peak_r;
    s_levels.span_left = frames ? (uint16_t)((int32_t)max_l - min_l) : 0;
    s_levels.span_right = frames ? (uint16_t)((int32_t)max_r - min_r) : 0;
    s_levels.equal_permille = frames ? (uint16_t)(equal_count * 1000 / frames) : 1000;
    s_levels.clipped_permille_left = frames ? (uint16_t)(clipped_l * 1000 / frames) : 1000;
    s_levels.clipped_permille_right = frames ? (uint16_t)(clipped_r * 1000 / frames) : 1000;
    s_levels.first_left = frames ? stereo[1] : 0;
    s_levels.first_right = frames ? stereo[0] : 0;
    s_levels.frames += frames;
    portEXIT_CRITICAL(&s_levels_lock);
    size_t sent = xStreamBufferSend(s_ring, mono, frames * sizeof(int16_t), 0);
    portENTER_CRITICAL(&s_stats_lock);
    s_stats.captured_samples += frames;
    s_stats.queued_samples += sent / sizeof(int16_t);
    if (sent < frames * sizeof(int16_t)) {
      s_stats.dropped_samples +=
          (frames * sizeof(int16_t) - sent) / sizeof(int16_t);
      s_stats.overflow_events++;
    }
    portEXIT_CRITICAL(&s_stats_lock);
    if (sent < frames * sizeof(int16_t)) {
      ESP_LOGW(TAG, "环形缓冲溢出,丢 %u 字节(写卡跟不上?)",
               (unsigned)(frames * sizeof(int16_t) - sent));
    }
  }
}

esp_err_t audio_pdm_init(void) {
  gpio_config_t ldo = {.pin_bit_mask = 1ULL << PIN_MIC_LDO_EN, .mode = GPIO_MODE_OUTPUT};
  esp_err_t err = gpio_config(&ldo);
  if (err != ESP_OK) return err;
  gpio_set_level(PIN_MIC_LDO_EN, 0);

  // 环形缓冲放 PSRAM(N16R8 有 8MB,主 RAM 留给 WiFi/LWIP)
  s_ring = xStreamBufferCreateWithCaps(AUDIO_RING_BYTES, 1, MALLOC_CAP_SPIRAM);
  if (!s_ring) return ESP_ERR_NO_MEM;

  // ESP32-S3 的硬件 PDM-to-PCM 转换器位于 I2S0，避免 AUTO 被其它通道占用后选错。
  i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  err = i2s_new_channel(&chan, NULL, &s_rx);
  if (err != ESP_OK) return err;
  i2s_pdm_rx_config_t pdm = {
    .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
    .slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {.clk = PIN_PDM_CLK, .din = PIN_PDM_DATA},
  };
  // ESP-IDF 默认 8S 会产生 16k * 64 = 1.024MHz，落在 IM72D128
  // 规定的 850kHz 与 1.2MHz 工作区间之间。16S 保持 PCM 16kHz 不变，
  // 同时把 PDM 时钟提升到 2.048MHz（芯片的 2.0~2.6MHz 高性能区间）。
  pdm.clk_cfg.dn_sample_mode = I2S_PDM_DSR_16S;
  err = i2s_channel_init_pdm_rx_mode(s_rx, &pdm);
  if (err != ESP_OK) return err;
  ESP_LOGI(TAG, "PDM RX: CLK=%uHz GPIO%d, DATA=GPIO%d, LDO_EN=GPIO%d, stereo 16-bit/%uHz frontend=dualmic-r3 legacy_tap_gain=%ux",
           (unsigned)PDM_CLOCK_HZ, PIN_PDM_CLK, PIN_PDM_DATA, PIN_MIC_LDO_EN,
           (unsigned)AUDIO_SAMPLE_RATE, (unsigned)AUDIO_PDM_PCM_GAIN);

  if (xTaskCreatePinnedToCore(capture_task, "audio_cap", 4096, NULL, 18, &s_task, 1) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}

esp_err_t audio_pdm_start(void) {
  gpio_set_level(PIN_MIC_LDO_EN, 1);
  vTaskDelay(pdMS_TO_TICKS(2));                 // 先让受控电源建立，再送 PDM 时钟
  s_muted = true;
  esp_err_t err = i2s_channel_enable(s_rx);
  if (err != ESP_OK) {
    gpio_set_level(PIN_MIC_LDO_EN, 0);
    return err;
  }
  s_running = true;                             // 后台持续读取并丢弃启动瞬态
  vTaskDelay(pdMS_TO_TICKS(MIC_STARTUP_MS));    // 手册：VDD+CLK 后最长 50ms 稳定
  xStreamBufferReset(s_ring);
  portENTER_CRITICAL(&s_levels_lock);
  memset(&s_levels, 0, sizeof(s_levels));
  portEXIT_CRITICAL(&s_levels_lock);
  portENTER_CRITICAL(&s_stats_lock);
  memset(&s_stats, 0, sizeof(s_stats));
  portEXIT_CRITICAL(&s_stats_lock);
  dmfe_reset();
  s_frontend_cal_logged = false;
  s_muted = false;
  return ESP_OK;
}

void audio_pdm_stop(void) {
  s_running = false;
  i2s_channel_disable(s_rx);
  gpio_set_level(PIN_MIC_LDO_EN, 0);
  dmfe_realtime_stats_t frontend;
  dmfe_get_stats(&frontend);
  ESP_LOGI(TAG,
           "LY|AUDIO_FRONTEND|state=stopped profile=dualmic-r3 frames=%lu voice=%lu delay_valid=%lu gain=%.2f clips=%lu",
           (unsigned long)frontend.total_frames,
           (unsigned long)frontend.voice_frames,
           (unsigned long)frontend.delay_valid_frames,
           (double)frontend.enhancement_gain,
           (unsigned long)frontend.enhanced_clip_samples);
}

void audio_pdm_set_muted(bool muted) { s_muted = muted; }

void audio_pdm_set_stereo_tap(audio_pdm_stereo_tap_t tap, void *user_ctx) {
  portENTER_CRITICAL(&s_tap_lock);
  s_stereo_tap_ctx = user_ctx;
  s_stereo_tap = tap;
  portEXIT_CRITICAL(&s_tap_lock);
}

void audio_pdm_set_mono_tap(audio_pdm_mono_tap_t tap, void *user_ctx) {
  portENTER_CRITICAL(&s_tap_lock);
  s_mono_tap_ctx = user_ctx;
  s_mono_tap = tap;
  portEXIT_CRITICAL(&s_tap_lock);
}

size_t audio_pdm_read(int16_t *dst, size_t max_samples, uint32_t timeout_ms) {
  size_t got = xStreamBufferReceive(s_ring, dst, max_samples * sizeof(int16_t),
                                    pdMS_TO_TICKS(timeout_ms));
  return got / sizeof(int16_t);
}

bool audio_pdm_get_levels(audio_pdm_levels_t *out) {
  if (!out) return false;
  portENTER_CRITICAL(&s_levels_lock);
  *out = s_levels;
  portEXIT_CRITICAL(&s_levels_lock);
  return out->frames > 0;
}

bool audio_pdm_get_stream_stats(audio_pdm_stream_stats_t *out) {
  if (!out) return false;
  portENTER_CRITICAL(&s_stats_lock);
  *out = s_stats;
  portEXIT_CRITICAL(&s_stats_lock);
  return true;
}
