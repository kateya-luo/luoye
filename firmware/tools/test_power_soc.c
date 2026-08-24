#include <stdio.h>
#include "power_soc.h"

#define CHECK(expr) do { \
  if (!(expr)) { \
    fprintf(stderr, "CHECK failed at line %d: %s\n", __LINE__, #expr); \
    return 1; \
  } \
} while (0)

int main(void) {
  const int anchors[][2] = {
    {0, 0}, {10, 10}, {20, 13}, {30, 17}, {40, 22}, {50, 28},
    {60, 42}, {70, 58}, {80, 74}, {86, 86}, {100, 100},
  };
  for (unsigned i = 0; i < sizeof(anchors) / sizeof(anchors[0]); ++i) {
    CHECK(power_soc_calibrate_x256(anchors[i][0] * 256) == anchors[i][1]);
  }
  int previous = -1;
  for (int raw = 0; raw <= 100 * 256; ++raw) {
    int shown = power_soc_calibrate_x256(raw);
    CHECK(shown >= previous);
    CHECK(shown >= 0 && shown <= 100);
    previous = shown;
  }
  CHECK(power_soc_calibrate_x256(-1) == 0);
  CHECK(power_soc_calibrate_x256(101 * 256) == 100);
  puts("power SOC calibration tests passed");
  return 0;
}
