# Luoye V1.4.2 low-SOC recording access

V1.4.2 is a narrow maintenance release based on V1.4.1.  Its server
contract and networking behaviour remain aligned with ClearMeeting 0.21.0.

## User-visible battery behaviour

- Battery percentage is display information only.  A displayed 0%, 1%, 2%,
  3%, or 4% does not block recording or voice-todo capture.
- A percentage update never stops an active recording.
- While unplugged, the measured cell voltage supplies a calibrated low-end
  display floor: 5% at 3500 mV, 4% at 3450 mV, 3% at 3400 mV, 2% at
  3300 mV, 1% at 3150 mV, and 0% below 3150 mV.
- The existing monotonic discharge filter remains active, so a resting-voltage
  rebound cannot make the displayed percentage rise while discharging.

The low-end anchors come from `0%很耐用版本.csv`, captured on 2026-08-28.
That run first displayed 0% around 3457 mV and later reached 3061 mV before
USB power was attached.  The new tail prevents integer SOC rounding from
hiding that usable reserve as a long-lived 0% reading.

## Physical data-integrity guard

Percentage-based restrictions are removed, but the firmware still protects an
open WAV from a real brownout.  When unplugged cell voltage is at or below
3000 mV for three consecutive power polls (approximately 15 seconds), the
power manager asks the recording state machine to close the active WAV safely.
This event does not create a percentage latch and does not forbid starting a
recording merely because the UI says 0%.

## Unchanged from V1.4.1

- ClearMeeting server release 0.21.0 and `luoye-device-api/2`
- recording, upload, storage, Wi-Fi, and charging implementations
- clock refresh and dynamic battery refresh behaviour
- ESP-IDF 5.5.4 engineering build profile
