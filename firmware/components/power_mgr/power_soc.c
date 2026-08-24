#include "power_soc.h"

#include <stddef.h>

typedef struct {
  int raw_percent;
  int shown_percent;
} soc_anchor_t;

/* Derived from power.csv captured on 2026-08-20.  The default MAX17048 model
 * held too much percentage above 50%, then caught up sharply below it.  This
 * monotonic mapping moves that correction earlier while preserving both
 * endpoints and the well-behaved <=10% safety region. */
static const soc_anchor_t CALIBRATION[] = {
  {0, 0}, {10, 10}, {20, 13}, {30, 17}, {40, 22}, {50, 28},
  {60, 42}, {70, 58}, {80, 74}, {86, 86}, {100, 100},
};

int power_soc_calibrate_x256(int raw_soc_x256) {
  if (raw_soc_x256 <= 0) return 0;
  if (raw_soc_x256 >= 100 * 256) return 100;

  for (size_t i = 1; i < sizeof(CALIBRATION) / sizeof(CALIBRATION[0]); ++i) {
    int upper_raw = CALIBRATION[i].raw_percent * 256;
    if (raw_soc_x256 > upper_raw) continue;
    int lower_raw = CALIBRATION[i - 1].raw_percent * 256;
    int lower_shown = CALIBRATION[i - 1].shown_percent;
    int shown_span = CALIBRATION[i].shown_percent - lower_shown;
    int raw_span = upper_raw - lower_raw;
    int numerator = (raw_soc_x256 - lower_raw) * shown_span;
    return lower_shown + (numerator + raw_span / 2) / raw_span;
  }
  return 100;
}
