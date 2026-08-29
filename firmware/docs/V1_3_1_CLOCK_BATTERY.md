# Luoye V1.3.1 clock and battery maintenance release

V1.3.1 is based exactly on the clean V1.3.0 serial-upload release. It keeps
Server V0.20.2 compatibility and does not change SD recording, the 160 KiB
live upload geometry, the single uploader task, offline ranges or caption
semantics.

## Fixed behavior

- The standby home page still wakes once per minute with Wi-Fi suspended.
- Clock and battery now share one fixed 174 x 78 logical partial window.
- The SSD1681 no longer receives an unreliable tiny glyph-only auto-diff after
  panel sleep.
- A minute is acknowledged only after the panel driver reports `ESP_OK`.
- A failed transaction remains pending and is retried by the one-second UI
  scheduler.
- The existing half-hour whole-panel refresh remains unchanged.

## Battery behavior retained from V1.3.0

- MAX17048/BQ25186 are sampled every five seconds while the CPU is awake.
- Charge display rises at most one percent per 30 seconds.
- Discharge display falls at most one percent per 60 seconds, except for the
  existing low-voltage fast path.
- The home battery percentage is carried by the same minute window as the
  clock; the status page continues to refresh immediately when its displayed
  percentage changes.

## Serial acceptance

Normal minute update:

```text
LY|UI_CLOCK|refresh=clock+battery-partial ... result=ESP_OK
```

If a panel transaction fails, V1.3.1 must log `event=render_retry`, must not
advance the acknowledged minute, and must try again without enabling Wi-Fi.

Test at least 45 minutes in standby and cross both an ordinary ten-minute
boundary and a `:00` or `:30` full refresh boundary.
