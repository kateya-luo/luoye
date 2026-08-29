#pragma once

#include <stdbool.h>
#include <stdint.h>

#define POWER_SOC_VOLTAGE_WINDOW 5

/* Passive display-only battery estimator.  Charging never reads this state. */
typedef struct {
  int voltage_history[POWER_SOC_VOLTAGE_WINDOW];
  uint8_t voltage_count;
  uint8_t voltage_index;
  int filtered_mv;
  bool have_power_state;
  bool was_unplugged;
} power_soc_display_t;

/* Piecewise-linear 3.180 V = 0% ... 4.100 V = 100% display curve. */
int power_soc_from_voltage(int battery_mv);

void power_soc_display_init(power_soc_display_t *state);

/* Map the median-filtered voltage directly to display percentage.  A
 * plug/unplug transition clears the old voltage window so the first sample in
 * the new electrical state becomes authoritative immediately. */
int power_soc_display_update(power_soc_display_t *state,
                             int battery_mv,
                             bool unplugged);
