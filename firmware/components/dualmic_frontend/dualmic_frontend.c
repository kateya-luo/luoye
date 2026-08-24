#include "dualmic_frontend.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define DMFE_MAX_LAG 2
#define DMFE_HP_ALPHA 0.9615f
#define DMFE_TARGET_RMS 4300.0f
#define DMFE_MAX_SPEECH_GAIN 24.0f
#define DMFE_BASELINE_GAIN 8.0f
#define DMFE_LIMIT_KNEE 24000.0f
#define DMFE_LIMIT_CEILING 30000.0f
#define DMFE_CALIBRATION_FRAMES 50U
#define DMFE_VOICE_HANGOVER_FRAMES 10U

typedef struct {
  float hp_ch1[DMFE_FRAME_SAMPLES];
  float hp_ch2[DMFE_FRAME_SAMPLES];
  float beam[DMFE_FRAME_SAMPLES];
  float noise_frames[DMFE_CALIBRATION_FRAMES];
  float previous_ch1_x;
  float previous_ch1_y;
  float previous_ch2_x;
  float previous_ch2_y;
  float previous_mono_x;
  float previous_mono_y;
  double calibration_energy_ch1;
  double calibration_energy_ch2;
  uint64_t calibration_samples;
  float smoothed_delay;
  float current_gain;
  float previous_effective_gain;
  uint32_t voice_hangover;
  dmfe_realtime_stats_t stats;
} dmfe_state_t;

static dmfe_state_t s;

static float clampf_local(float value, float low, float high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

static int compare_float(const void *lhs, const void *rhs) {
  const float a = *(const float *)lhs;
  const float b = *(const float *)rhs;
  return (a > b) - (a < b);
}

static float channel_sample_linear(const int16_t *stereo,
                                   size_t frames,
                                   size_t channel,
                                   float position) {
  if (position <= 0.0f) return (float)stereo[channel];
  const float last = (float)(frames - 1U);
  if (position >= last) return (float)stereo[2U * (frames - 1U) + channel];
  const size_t index = (size_t)position;
  const float fraction = position - (float)index;
  const float a = (float)stereo[2U * index + channel];
  const float b = (float)stereo[2U * (index + 1U) + channel];
  return a + (b - a) * fraction;
}

static bool estimate_delay(const float *ch1,
                           const float *ch2,
                           size_t length,
                           float energy_threshold,
                           float *delay,
                           float *correlation) {
  double energy = 0.0;
  for (size_t i = 0; i < length; ++i) {
    energy += 0.5 * ((double)ch1[i] * ch1[i] + (double)ch2[i] * ch2[i]);
  }
  const float rms = length ? sqrtf((float)(energy / (double)length)) : 0.0f;
  if (rms < energy_threshold) return false;

  float correlations[2 * DMFE_MAX_LAG + 1];
  int best_lag = 0;
  float best_correlation = -2.0f;
  for (int lag = -DMFE_MAX_LAG; lag <= DMFE_MAX_LAG; ++lag) {
    const size_t begin = lag < 0 ? (size_t)(-lag) : 0U;
    const size_t end = lag > 0 ? length - (size_t)lag : length;
    double dot = 0.0;
    double power_ch1 = 0.0;
    double power_ch2 = 0.0;
    for (size_t i = begin; i < end; ++i) {
      const size_t j = (size_t)((int)i + lag);
      const double a = ch1[i];
      const double b = ch2[j];
      dot += a * b;
      power_ch1 += a * a;
      power_ch2 += b * b;
    }
    const double denominator = sqrt(power_ch1 * power_ch2);
    const float value = denominator > 1.0 ? (float)(dot / denominator) : 0.0f;
    correlations[lag + DMFE_MAX_LAG] = value;
    if (value > best_correlation) {
      best_correlation = value;
      best_lag = lag;
    }
  }
  if (best_correlation < 0.12f) return false;

  float fractional = 0.0f;
  if (best_lag > -DMFE_MAX_LAG && best_lag < DMFE_MAX_LAG) {
    const size_t center = (size_t)(best_lag + DMFE_MAX_LAG);
    const float left = correlations[center - 1U];
    const float middle = correlations[center];
    const float right = correlations[center + 1U];
    const float denominator = left - 2.0f * middle + right;
    if (fabsf(denominator) > 1.0e-6f) {
      fractional = 0.5f * (left - right) / denominator;
      fractional = clampf_local(fractional, -0.5f, 0.5f);
    }
  }
  *delay = (float)best_lag + fractional;
  *correlation = best_correlation;
  return true;
}

static float soft_limit(float sample) {
  const float magnitude = fabsf(sample);
  if (magnitude >= DMFE_LIMIT_CEILING) ++s.stats.enhanced_clip_samples;
  float limited = magnitude;
  if (limited > DMFE_LIMIT_KNEE) {
    limited = DMFE_LIMIT_KNEE + 0.25f * (limited - DMFE_LIMIT_KNEE);
  }
  if (limited > DMFE_LIMIT_CEILING) limited = DMFE_LIMIT_CEILING;
  return sample < 0.0f ? -limited : limited;
}

void dmfe_reset(void) {
  memset(&s, 0, sizeof(s));
  s.current_gain = DMFE_BASELINE_GAIN;
  s.previous_effective_gain = DMFE_BASELINE_GAIN;
  s.stats.balance_gain_ch1 = 1.0f;
  s.stats.balance_gain_ch2 = 1.0f;
  s.stats.calibration_noise_rms = 20.0f;
  s.stats.enhancement_gain = DMFE_BASELINE_GAIN;
}

bool dmfe_process_right_left(const int16_t *raw_right_left,
                             size_t frames,
                             int16_t *enhanced_mono) {
  if (!raw_right_left || !enhanced_mono || frames <= (2U * DMFE_MAX_LAG) ||
      frames > DMFE_FRAME_SAMPLES) {
    return false;
  }

  const uint32_t frame_number = s.stats.total_frames + 1U;
  for (size_t i = 0; i < frames; ++i) {
    const float ch1 = (float)raw_right_left[2U * i];
    const float ch2 = (float)raw_right_left[2U * i + 1U];
    const float hp1 = ch1 - s.previous_ch1_x + DMFE_HP_ALPHA * s.previous_ch1_y;
    const float hp2 = ch2 - s.previous_ch2_x + DMFE_HP_ALPHA * s.previous_ch2_y;
    s.previous_ch1_x = ch1;
    s.previous_ch1_y = hp1;
    s.previous_ch2_x = ch2;
    s.previous_ch2_y = hp2;
    if (frame_number <= DMFE_CALIBRATION_FRAMES) {
      s.calibration_energy_ch1 += (double)hp1 * hp1;
      s.calibration_energy_ch2 += (double)hp2 * hp2;
      ++s.calibration_samples;
    }
    s.hp_ch1[i] = hp1 * s.stats.balance_gain_ch1;
    s.hp_ch2[i] = hp2 * s.stats.balance_gain_ch2;
  }

  float measured_delay = 0.0f;
  float measured_correlation = 0.0f;
  const float delay_threshold = fmaxf(
      12.0f, 0.65f * s.stats.calibration_noise_rms);
  if (estimate_delay(s.hp_ch1, s.hp_ch2, frames, delay_threshold,
                     &measured_delay, &measured_correlation)) {
    if (s.stats.delay_valid_frames == 0U) {
      s.smoothed_delay = measured_delay;
    } else {
      s.smoothed_delay = 0.82f * s.smoothed_delay + 0.18f * measured_delay;
    }
    s.smoothed_delay = clampf_local(s.smoothed_delay, -2.0f, 2.0f);
    s.stats.delay_samples = s.smoothed_delay;
    s.stats.correlation = measured_correlation;
    ++s.stats.delay_valid_frames;
  }

  double beam_squares = 0.0;
  for (size_t i = 0; i < frames; ++i) {
    const float ch1 = s.stats.balance_gain_ch1 *
                      (float)raw_right_left[2U * i];
    const float ch2_position = (float)i + s.smoothed_delay;
    const float ch2 = s.stats.balance_gain_ch2 *
                      channel_sample_linear(raw_right_left, frames, 1U,
                                            ch2_position);
    const float mixed = 0.5f * (ch1 + ch2);
    const float filtered = mixed - s.previous_mono_x +
                           DMFE_HP_ALPHA * s.previous_mono_y;
    s.previous_mono_x = mixed;
    s.previous_mono_y = filtered;
    s.beam[i] = filtered;
    beam_squares += (double)filtered * filtered;
  }
  const float frame_rms = sqrtf((float)(beam_squares / (double)frames));

  if (frame_number <= DMFE_CALIBRATION_FRAMES) {
    s.noise_frames[frame_number - 1U] = frame_rms;
  }
  if (frame_number == DMFE_CALIBRATION_FRAMES) {
    if (s.calibration_samples > 0U && s.calibration_energy_ch1 > 1.0 &&
        s.calibration_energy_ch2 > 1.0) {
      const float rms1 = sqrtf((float)(s.calibration_energy_ch1 /
                                       (double)s.calibration_samples));
      const float rms2 = sqrtf((float)(s.calibration_energy_ch2 /
                                       (double)s.calibration_samples));
      s.stats.balance_gain_ch1 = clampf_local(sqrtf(rms2 / rms1),
                                             0.7071f, 1.4142f);
      s.stats.balance_gain_ch2 = clampf_local(sqrtf(rms1 / rms2),
                                             0.7071f, 1.4142f);
    }
    float sorted[DMFE_CALIBRATION_FRAMES];
    memcpy(sorted, s.noise_frames, sizeof(sorted));
    qsort(sorted, DMFE_CALIBRATION_FRAMES, sizeof(sorted[0]), compare_float);
    s.stats.calibration_noise_rms = clampf_local(
        sorted[DMFE_CALIBRATION_FRAMES / 5U], 12.0f, 4000.0f);
    s.stats.calibrated = true;
  }

  float desired_gain = DMFE_BASELINE_GAIN;
  if (frame_number > DMFE_CALIBRATION_FRAMES) {
    const float voice_threshold = fmaxf(
        18.0f, 1.25f * s.stats.calibration_noise_rms + 3.0f);
    const bool detected_voice = frame_rms >= voice_threshold;
    if (detected_voice) {
      s.voice_hangover = DMFE_VOICE_HANGOVER_FRAMES;
    } else if (s.voice_hangover > 0U) {
      --s.voice_hangover;
    }
    const bool voice = detected_voice || s.voice_hangover > 0U;
    if (voice) {
      ++s.stats.voice_frames;
      desired_gain = clampf_local(DMFE_TARGET_RMS / fmaxf(frame_rms, 1.0f),
                                  1.0f, DMFE_MAX_SPEECH_GAIN);
    }
    if (!detected_voice &&
        frame_rms < s.stats.calibration_noise_rms * 1.4f) {
      s.stats.calibration_noise_rms =
          0.99f * s.stats.calibration_noise_rms +
          0.01f * fmaxf(frame_rms, 8.0f);
    }
  }

  const float gain_alpha = desired_gain < s.current_gain ? 0.35f : 0.25f;
  s.current_gain += gain_alpha * (desired_gain - s.current_gain);
  for (size_t i = 0; i < frames; ++i) {
    const float progress = (float)(i + 1U) / (float)frames;
    const float gain = s.previous_effective_gain + progress *
        (s.current_gain - s.previous_effective_gain);
    enhanced_mono[i] = (int16_t)lrintf(soft_limit(s.beam[i] * gain));
  }
  s.previous_effective_gain = s.current_gain;
  s.stats.enhancement_gain = s.current_gain;
  s.stats.total_frames = frame_number;
  return true;
}

void dmfe_get_stats(dmfe_realtime_stats_t *out) {
  if (out) *out = s.stats;
}
