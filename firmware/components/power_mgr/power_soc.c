#include "power_soc.h"

#include <stddef.h>
#include <string.h>

typedef struct {
  int battery_mv;
  int shown_percent;
} voltage_anchor_t;

/* User-facing curve agreed from the 2026-08-28 full-discharge capture.
 * This is deliberately a passive display curve, not a charge or feature
 * control input.  The wide low-voltage tail keeps 0% close to real depletion
 * while following the increasingly steep Li-polymer discharge knee. */
static const voltage_anchor_t VOLTAGE_CURVE[] = {
  {3180, 0}, {3200, 1}, {3250, 2}, {3300, 3}, {3350, 4},
  {3400, 5}, {3450, 7}, {3500, 10}, {3550, 15}, {3600, 22},
  {3650, 30}, {3700, 39}, {3750, 48}, {3800, 57}, {3850, 66},
  {3900, 75}, {3950, 83}, {4000, 90}, {4050, 96}, {4100, 100},
};

int power_soc_from_voltage(int battery_mv) {
  if (battery_mv <= VOLTAGE_CURVE[0].battery_mv) return 0;
  const size_t count = sizeof(VOLTAGE_CURVE) / sizeof(VOLTAGE_CURVE[0]);
  if (battery_mv >= VOLTAGE_CURVE[count - 1].battery_mv) return 100;

  for (size_t i = 1; i < count; ++i) {
    const voltage_anchor_t lower = VOLTAGE_CURVE[i - 1];
    const voltage_anchor_t upper = VOLTAGE_CURVE[i];
    if (battery_mv > upper.battery_mv) continue;
    const int mv_span = upper.battery_mv - lower.battery_mv;
    const int soc_span = upper.shown_percent - lower.shown_percent;
    const int numerator = (battery_mv - lower.battery_mv) * soc_span;
    return lower.shown_percent + (numerator + mv_span / 2) / mv_span;
  }
  return 100;
}

static int median_voltage(const int *values, unsigned count) {
  int sorted[POWER_SOC_VOLTAGE_WINDOW];
  if (count == 0) return -1;
  if (count > POWER_SOC_VOLTAGE_WINDOW) count = POWER_SOC_VOLTAGE_WINDOW;
  for (unsigned i = 0; i < count; ++i) sorted[i] = values[i];
  for (unsigned i = 1; i < count; ++i) {
    int value = sorted[i];
    unsigned j = i;
    while (j > 0 && sorted[j - 1] > value) {
      sorted[j] = sorted[j - 1];
      --j;
    }
    sorted[j] = value;
  }
  if ((count & 1U) != 0U) return sorted[count / 2];
  return (sorted[count / 2 - 1] + sorted[count / 2] + 1) / 2;
}

void power_soc_display_init(power_soc_display_t *state) {
  if (!state) return;
  memset(state, 0, sizeof(*state));
  state->filtered_mv = -1;
}

static void reset_voltage_window(power_soc_display_t *state) {
  state->voltage_count = 0;
  state->voltage_index = 0;
  state->filtered_mv = -1;
}

static int update_filtered_voltage(power_soc_display_t *state,
                                   int battery_mv) {
  state->voltage_history[state->voltage_index] = battery_mv;
  state->voltage_index = (uint8_t)((state->voltage_index + 1U) %
                                   POWER_SOC_VOLTAGE_WINDOW);
  if (state->voltage_count < POWER_SOC_VOLTAGE_WINDOW) state->voltage_count++;
  state->filtered_mv = median_voltage(state->voltage_history,
                                      state->voltage_count);
  return state->filtered_mv;
}

int power_soc_display_update(power_soc_display_t *state,
                             int battery_mv,
                             bool unplugged) {
  if (!state || battery_mv <= 0) return -1;

  bool power_changed = state->have_power_state &&
                       state->was_unplugged != unplugged;
  if (power_changed) reset_voltage_window(state);
  int filtered_mv = update_filtered_voltage(state, battery_mv);
  state->have_power_state = true;
  state->was_unplugged = unplugged;
  return power_soc_from_voltage(filtered_mv);
}
