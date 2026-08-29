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
    {3180, 0}, {3200, 1}, {3250, 2}, {3300, 3}, {3350, 4},
    {3400, 5}, {3450, 7}, {3500, 10}, {3550, 15}, {3600, 22},
    {3650, 30}, {3700, 39}, {3750, 48}, {3800, 57}, {3850, 66},
    {3900, 75}, {3950, 83}, {4000, 90}, {4050, 96}, {4100, 100},
  };
  for (unsigned i = 0; i < sizeof(anchors) / sizeof(anchors[0]); ++i) {
    CHECK(power_soc_from_voltage(anchors[i][0]) == anchors[i][1]);
  }
  int previous = -1;
  for (int mv = 2800; mv <= 4300; ++mv) {
    int shown = power_soc_from_voltage(mv);
    CHECK(shown >= previous);
    CHECK(shown >= 0 && shown <= 100);
    previous = shown;
  }
  CHECK(power_soc_from_voltage(3000) == 0);
  CHECK(power_soc_from_voltage(4200) == 100);
  CHECK(power_soc_from_voltage(3225) == 2);
  CHECK(power_soc_from_voltage(3975) == 87);

  power_soc_display_t state;

  /* The first valid sample is authoritative in either power state. */
  power_soc_display_init(&state);
  CHECK(power_soc_display_update(&state, 4177, true) == 100);
  power_soc_display_init(&state);
  CHECK(power_soc_display_update(&state, 4150, false) == 100);

  /* The rolling median filters samples, without an artificial SOC rate. */
  power_soc_display_init(&state);
  CHECK(power_soc_display_update(&state, 3800, true) == 57);
  CHECK(power_soc_display_update(&state, 3900, true) == 66);
  CHECK(power_soc_display_update(&state, 3900, true) == 75);
  CHECK(power_soc_display_update(&state, 3900, true) == 75);
  CHECK(power_soc_display_update(&state, 3900, true) == 75);

  /* Plug/unplug clears old polarization samples and maps the new voltage now. */
  power_soc_display_init(&state);
  CHECK(power_soc_display_update(&state, 4150, false) == 100);
  CHECK(power_soc_display_update(&state, 3900, true) == 75);
  CHECK(state.filtered_mv == 3900);

  /* Rebooting cannot invent a different percentage for the same voltage. */
  power_soc_display_init(&state);
  int before_reboot = power_soc_display_update(&state, 3975, false);
  power_soc_display_init(&state);
  int after_reboot = power_soc_display_update(&state, 3975, false);
  CHECK(before_reboot == 87);
  CHECK(after_reboot == before_reboot);

  puts("direct voltage battery-display tests passed");
  return 0;
}
