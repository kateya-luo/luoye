// audio_pdm.h — 双 PDM 麦克风采集 → 16kHz 单声道 PCM 流缓冲
// 数据流:I2S PDM RX(立体声) → 混合为单声道 → StreamBuffer(PSRAM) → storage_sd 拉取写卡
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "esp_err.h"

#define AUDIO_SAMPLE_RATE  16000
#define AUDIO_RING_BYTES   (256 * 1024)   // PSRAM 环形缓冲 ≈ 8s @16k/16bit,断卡抖动余量
#define AUDIO_PDM_PCM_GAIN 8U             // PDM-to-PCM 数字增益 8x ≈ +18dB

typedef struct {
  uint16_t mean_abs_left;    // 最近一个采集块的平均绝对幅度
  uint16_t mean_abs_right;
  uint16_t peak_left;        // 最近一个采集块的峰值
  uint16_t peak_right;
  uint16_t span_left;        // latest block max-min; zero indicates stuck data
  uint16_t span_right;
  uint16_t equal_permille;   // L==R samples per thousand; 1000 indicates mirrored channels
  uint16_t clipped_permille_left;
  uint16_t clipped_permille_right;
  int16_t first_left;         // 最近一块首帧原始 PCM，便于识别固定异常码
  int16_t first_right;
  uint32_t frames;
} audio_pdm_levels_t;

typedef struct {
  uint64_t captured_samples;
  uint64_t queued_samples;
  uint64_t dropped_samples;
  uint32_t overflow_events;
} audio_pdm_stream_stats_t;

// 诊断旁路：每块 I2S PDM-to-PCM 原始立体声数据到达时调用。
// ESP32-S3 DMA 顺序为 right,left；回调运行在音频采集任务中，必须快速返回。
typedef void (*audio_pdm_stereo_tap_t)(const int16_t *right_left,
                                       size_t frames,
                                       void *user_ctx);
/* Enhanced mono tap used by auxiliary voice capture such as spoken todos. */
typedef void (*audio_pdm_mono_tap_t)(const int16_t *mono,
                                     size_t frames,
                                     void *user_ctx);

esp_err_t audio_pdm_init(void);
esp_err_t audio_pdm_start(void);          // 开麦 LDO + 启动 I2S,开始往环形缓冲写
void audio_pdm_stop(void);                // 停 I2S + 关麦 LDO(缓冲余量保留给写卡任务排空)
void audio_pdm_set_muted(bool muted);     // 暂停:继续采集但丢弃(保持时钟稳定)
void audio_pdm_set_stereo_tap(audio_pdm_stereo_tap_t tap, void *user_ctx);
void audio_pdm_set_mono_tap(audio_pdm_mono_tap_t tap, void *user_ctx);
size_t audio_pdm_read(int16_t *dst, size_t max_samples, uint32_t timeout_ms);  // 写卡任务调用
bool audio_pdm_get_levels(audio_pdm_levels_t *out);  // 产测/诊断：确认左右麦都在出数据
bool audio_pdm_get_stream_stats(audio_pdm_stream_stats_t *out);
