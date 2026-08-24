#pragma once

#include <stdint.h>

/* Application-specific correction measured from the 1000mAh Luoye cell.
 * Input uses the MAX17048 SOC register's native 1/256 percent unit. */
int power_soc_calibrate_x256(int raw_soc_x256);
