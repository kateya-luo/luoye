#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "storage_format.h"

static int failures;
#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)

static uint32_t read_le32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

int main(void) {
  uint8_t header[LUOYE_WAV_HEADER_BYTES];
  luoye_wav_build_header(header, 16000, 1, 16, 32000);
  CHECK(memcmp(header, "RIFF", 4) == 0);
  CHECK(memcmp(header + 8, "WAVEfmt ", 8) == 0);
  CHECK(memcmp(header + 36, "data", 4) == 0);
  CHECK(read_le32(header + 4) == 32036);
  CHECK(read_le32(header + 24) == 16000);
  CHECK(read_le32(header + 28) == 32000);
  CHECK(read_le32(header + 40) == 32000);

  luoye_wav_repair_plan_t p = luoye_wav_plan_repair(44 + 101);
  CHECK(p.pcm_bytes == 100);
  CHECK(p.repaired_size == 144);
  CHECK(p.needs_truncate);

  p = luoye_wav_plan_repair(44 + 100);
  CHECK(p.pcm_bytes == 100);
  CHECK(p.repaired_size == 144);
  CHECK(!p.needs_truncate);

  p = luoye_wav_plan_repair(12);
  CHECK(p.pcm_bytes == 0);
  CHECK(p.repaired_size == 44);
  CHECK(p.needs_truncate);

  const uint8_t complete[] = "{\"a\":1}\n{\"b\":2}\n";
  CHECK(luoye_jsonl_complete_prefix(complete, sizeof(complete) - 1) ==
        sizeof(complete) - 1);
  const uint8_t torn[] = "{\"a\":1}\n{\"b\":";
  CHECK(luoye_jsonl_complete_prefix(torn, sizeof(torn) - 1) == 8);
  const uint8_t no_newline[] = "{\"a\":1}";
  CHECK(luoye_jsonl_complete_prefix(no_newline, sizeof(no_newline) - 1) == 0);

  printf(failures ? "%d storage-format checks failed\n"
                  : "storage-format checks passed\n", failures);
  return failures ? 1 : 0;
}
