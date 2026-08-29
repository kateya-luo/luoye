# Luoye V1.4.1 / ClearMeeting V0.21.0 alignment

V1.4.1 is a narrow compatibility release based on the proven V1.3.1
maintenance branch.  It does not import the experimental V2.x storage or
dual-task upload work.

## Runtime contract

- Device API remains `luoye-device-api/2`.
- Required server release is ClearMeeting `0.21.0`.
- Build-info must advertise `transcript_only_live_v1` and
  `canonical_offline_diarization_v2` in addition to the existing upload,
  pairing, agenda, todo and storage capabilities.
- Live recording displays captions only.  The obsolete rolling-minutes parser,
  cache and recording-page switch have been removed.
- A short MARK press during recording still uploads a timeline mark.  A long
  MARK press during recording is intentionally inert.  Standby long MARK still
  records a voice todo.

## Post-meeting boundary

The recorder owns reliable audio delivery, not server-side finalization.  The
existing upload ACK rules are retained and explicitly match V0.21.0:

1. All audio bytes and marks must be acknowledged.
2. `/end` or `/complete` must report no missing chunks/ranges.
3. `processing` is a successful durable handoff, just like `done`; the local
   upload queue can settle and the server continues canonical ASR and final
   speaker diarization independently.
4. `transcript_ready`, template selection and DeepSeek minutes generation are
   web/server concerns and are never polled by the recorder.

This prevents a several-minute canonical finalization job from holding the
device on its upload screen or keeping the upload lane busy.

## Deliberately unchanged from V1.3.1

- single uploader task;
- 160 KiB live chunks and 10 MiB range repair;
- SD recording, storage format and deletion-after-cloud-ACK rules;
- TCP/HTTP/SD performance profile;
- caption polling and five-second recording-page refresh;
- clock/battery partial refresh fix;
- power, agenda, pairing and standby scheduling.

## Acceptance checks

- V0.21.0 build-info is accepted; a server without the two V0.21 capabilities
  is rejected instead of silently restoring obsolete rolling behavior.
- Recording starts on captions and can never switch to rolling minutes.
- Short MARK still increments the meeting mark count.
- After a complete upload returns `processing`, local data follows the existing
  cloud-accepted cleanup policy without waiting for `transcript_ready`.
- All host tests, static checks and an ESP-IDF 5.5.4 clean build must pass.
