#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LUOYE_LIVE_SESSION_ID_BYTES 72
#define LUOYE_LIVE_MEETING_BYTES    512
#define LUOYE_LIVE_SOURCE_BYTES     256
#define LUOYE_LIVE_TRANSLATED_BYTES 384
#define LUOYE_LIVE_LANGUAGE_BYTES   16

typedef enum {
  LUOYE_LIVE_NONE = 0,
  LUOYE_LIVE_MEETING,
  LUOYE_LIVE_TRANSLATION,
} luoye_live_kind_t;

typedef struct {
  char client_session_id[LUOYE_LIVE_SESSION_ID_BYTES];
  char server_session_id[LUOYE_LIVE_SESSION_ID_BYTES];
  uint32_t revision;
  uint32_t contiguous_pcm_bytes;
  luoye_live_kind_t kind;
  bool final;
  bool failed;
  char meeting_text[LUOYE_LIVE_MEETING_BYTES];
  char source_text[LUOYE_LIVE_SOURCE_BYTES];
  char translated_text[LUOYE_LIVE_TRANSLATED_BYTES];
  char source_language[LUOYE_LIVE_LANGUAGE_BYTES];
  char target_language[LUOYE_LIVE_LANGUAGE_BYTES];
  bool speaker_enabled;
  uint16_t speaker_labeled_segments;
  uint8_t speaker_count;
} luoye_live_result_t;

/* A reply may advance only monotonically and never beyond acknowledged PCM. */
bool luoye_live_cursor_accept(uint32_t current_revision,
                              uint32_t current_contiguous_pcm_bytes,
                              uint32_t acknowledged_pcm_bytes,
                              uint32_t incoming_revision,
                              uint32_t incoming_contiguous_pcm_bytes);

/* Strictly bounded copy used after the JSON layer has validated field types. */
bool luoye_live_set_text(char *destination, size_t destination_size,
                         const char *source);

/* Appends one new caption unit while retaining the newest complete UTF-8
   suffix when the fixed display buffer is full. */
bool luoye_live_append_text(char *destination, size_t destination_size,
                            const char *source);

/* Builds the GET suffix without allowing an unbounded query string. */
bool luoye_live_query(char *out, size_t out_size, uint32_t after_revision);
