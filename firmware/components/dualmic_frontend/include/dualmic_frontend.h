#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DMFE_SAMPLE_RATE 16000U
#define DMFE_FRAME_SAMPLES 320U

typedef struct {
  float balance_gain_ch1;
  float balance_gain_ch2;
  float calibration_noise_rms;
  float delay_samples;
  float correlation;
  float enhancement_gain;
  uint32_t total_frames;
  uint32_t voice_frames;
  uint32_t delay_valid_frames;
  uint32_t enhanced_clip_samples;
  bool calibrated;
} dmfe_realtime_stats_t;

/*
 * Stateful production front end for one 20 ms block at 16 kHz.
 * Input order is the ESP32-S3 PDM DMA order used by this board: right, left.
 * Output is enhanced mono PCM16.  Call dmfe_reset() for every new recording.
 */
void dmfe_reset(void);
bool dmfe_process_right_left(const int16_t *raw_right_left,
                             size_t frames,
                             int16_t *enhanced_mono);
void dmfe_get_stats(dmfe_realtime_stats_t *out);

#ifdef __cplusplus
}
#endif
