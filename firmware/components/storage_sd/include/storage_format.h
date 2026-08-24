#pragma once

#include <stddef.h>
#include <stdint.h>

#define LUOYE_WAV_HEADER_BYTES 44U

typedef struct {
  uint64_t original_size;
  uint64_t repaired_size;
  uint32_t pcm_bytes;
  int needs_truncate;
  int needs_header;
} luoye_wav_repair_plan_t;

/* Pure helpers shared by firmware recovery and the host fault-injection tests. */
void luoye_wav_build_header(uint8_t out[LUOYE_WAV_HEADER_BYTES],
                            uint32_t sample_rate,
                            uint16_t channels,
                            uint16_t bits_per_sample,
                            uint32_t pcm_bytes);
luoye_wav_repair_plan_t luoye_wav_plan_repair(uint64_t file_size);
size_t luoye_jsonl_complete_prefix(const uint8_t *data, size_t size);
