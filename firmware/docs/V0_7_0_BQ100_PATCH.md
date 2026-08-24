# Luoye v0.7.0 low-complexity rollback patch

This branch stays on the verified `luoye-fw-v0.7.0-engineering-lan` source
baseline. It does not include ESP-SR AFE or additional feed/fetch tasks.

Charge policy:

- BQ25186 is initialized and verified at boot; GPIO38 explicitly pulls CE_N low.
- Before a valid MAX17048 SOC sample, charging starts conservatively at 200mA.
- SOC below 80%: 1000mA battery-charge target.
- SOC 80% through 89%: 500mA battery-charge target.
- SOC 90% through 99%: 200mA battery-charge target.
- Input current limit stays at 1050mA in every band because it includes both
  the battery current and the live ESP32/WiFi/display/SD system load.
- At 100%, CHG_DIS stops charging. At 99%, charging is enabled again.
- An invalid MAX17048 read keeps the previous policy.
- Charge/full UI state changes require two valid five-second samples. A transient
  BQ I2C error does not publish a UI state transition.
- The BQ register watchdog is disabled so the selected policy persists.
- Periodic logs expose ILIM, VDPPM, VINDPM and thermal-regulation status bits.
- SYS uses battery-tracking regulation (`VBAT + 225mV`, minimum 3.8V) instead of
  the 4.5V reset default. The downstream TPS63001 remains the card-rail
  regulator. DPPM protection stays enabled.

Network route policy:

- `TP-LINK_184F` uses `http://192.168.31.183`.
- Every other WiFi uses the compiled public endpoint
  `http://clearmeeting.chat:34567`.

Time display:

- RTC and system timestamps remain UTC.
- The home page consistently uses UTC+8, including when agenda metadata is
  missing or stale.

The audio path remains the original v0.7.0 single `audio_cap` task with hardware
PDM-to-PCM, software gain and L/R averaging. ESP-SR is absent.
