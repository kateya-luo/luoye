# UI refresh and replacement SD-card behavior

## E-paper refresh policy

- The home clock wakes on each minute boundary and refreshes only the clock
  rectangle (`24,24,152,58`).
- At minute `00` and `30`, the visible clock page uses a FULL waveform to clear
  accumulated partial-refresh ghosting.
- Reconnecting Wi-Fi and refreshing the account binding while the home page is
  visible update cached state only; they do not repaint the panel.
- Entering/leaving the settings status page uses a full-screen partial window.
  Subsequent network, cloud, storage and battery changes refresh only the
  status area (`0,28,200,170`).
- The active recording body keeps its five-second partial refresh and uses a
  whole-panel FAST refresh every five minutes to bound accumulated ghosting.

## Blank or replacement SD cards

Boot follows this order:

1. Try four normal FAT/FAT32 mounts, including the existing SPI idle-clock
   recovery. Automatic formatting is always disabled.
2. Create `/rec` and `/diag` when missing.
3. Perform a durable create/write/fsync/close/delete probe.
4. Create `/luoye-card.json` on a card not previously initialized by Luoye.
5. Recover interrupted sessions before enabling recording.

ESP-IDF v5.5.4 has exFAT disabled in its FatFs component. An exFAT, NTFS or
unformatted card is left untouched and reported unavailable. Format a truly
blank replacement card as FAT32 on a computer before installing it.
