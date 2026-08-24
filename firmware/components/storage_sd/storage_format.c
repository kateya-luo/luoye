#include "storage_format.h"
#include <limits.h>
#include <string.h>

static void le16(uint8_t *p, uint16_t value) {
  p[0] = (uint8_t)value;
  p[1] = (uint8_t)(value >> 8);
}

static void le32(uint8_t *p, uint32_t value) {
  p[0] = (uint8_t)value;
  p[1] = (uint8_t)(value >> 8);
  p[2] = (uint8_t)(value >> 16);
  p[3] = (uint8_t)(value >> 24);
}

void luoye_wav_build_header(uint8_t out[LUOYE_WAV_HEADER_BYTES],
                            uint32_t sample_rate,
                            uint16_t channels,
                            uint16_t bits_per_sample,
                            uint32_t pcm_bytes) {
  const uint16_t block_align = (uint16_t)(channels * (bits_per_sample / 8U));
  const uint32_t byte_rate = sample_rate * block_align;
  memset(out, 0, LUOYE_WAV_HEADER_BYTES);
  memcpy(out + 0, "RIFF", 4);
  le32(out + 4, 36U + pcm_bytes);
  memcpy(out + 8, "WAVEfmt ", 8);
  le32(out + 16, 16U);
  le16(out + 20, 1U);
  le16(out + 22, channels);
  le32(out + 24, sample_rate);
  le32(out + 28, byte_rate);
  le16(out + 32, block_align);
  le16(out + 34, bits_per_sample);
  memcpy(out + 36, "data", 4);
  le32(out + 40, pcm_bytes);
}

luoye_wav_repair_plan_t luoye_wav_plan_repair(uint64_t file_size) {
  luoye_wav_repair_plan_t plan = {
    .original_size = file_size,
    .repaired_size = LUOYE_WAV_HEADER_BYTES,
    .pcm_bytes = 0,
    .needs_truncate = file_size != LUOYE_WAV_HEADER_BYTES,
    .needs_header = 1,
  };
  if (file_size <= LUOYE_WAV_HEADER_BYTES) return plan;

  uint64_t pcm = (file_size - LUOYE_WAV_HEADER_BYTES) & ~1ULL;
  if (pcm > UINT32_MAX - 36U) pcm = (UINT32_MAX - 36U) & ~1ULL;
  plan.pcm_bytes = (uint32_t)pcm;
  plan.repaired_size = LUOYE_WAV_HEADER_BYTES + pcm;
  plan.needs_truncate = plan.repaired_size != file_size;
  return plan;
}

size_t luoye_jsonl_complete_prefix(const uint8_t *data, size_t size) {
  if (!data || !size) return 0;
  for (size_t i = size; i > 0; --i) {
    if (data[i - 1] == '\n') return i;
  }
  return 0;
}
