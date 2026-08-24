#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LUOYE_LIVE_SESSION_ID_BYTES 72
#define LUOYE_LIVE_MEETING_BYTES    512
#define LUOYE_LIVE_PARTIAL_MAX_BYTES 512
#define LUOYE_LIVE_PARTIAL_BYTES    (LUOYE_LIVE_PARTIAL_MAX_BYTES + 1)
#define LUOYE_LIVE_SOURCE_BYTES     256
#define LUOYE_LIVE_TRANSLATED_BYTES 384
#define LUOYE_LIVE_LANGUAGE_BYTES   16
#define LUOYE_LIVE_CHAPTER_TITLE_BYTES 128
#define LUOYE_LIVE_CHAPTER_ITEM_BYTES  192
#define LUOYE_LIVE_CAPTION_ID_BYTES    64
#define LUOYE_LIVE_CAPTION_TEXT_BYTES  (512 + 1)
#define LUOYE_LIVE_CAPTION_CACHE_ITEMS 16
#define LUOYE_LIVE_CAPTION_SEEN_ITEMS  64

typedef struct {
  char seg_id[LUOYE_LIVE_CAPTION_ID_BYTES];
  char text[LUOYE_LIVE_CAPTION_TEXT_BYTES];
} luoye_live_caption_t;

typedef struct {
  luoye_live_caption_t current[LUOYE_LIVE_CAPTION_CACHE_ITEMS];
  char seen[LUOYE_LIVE_CAPTION_SEEN_ITEMS][LUOYE_LIVE_CAPTION_ID_BYTES];
  uint8_t current_count;
  uint8_t seen_count;
  uint8_t seen_cursor;
} luoye_live_caption_cache_t;

typedef enum {
  LUOYE_LIVE_NONE = 0,
  LUOYE_LIVE_MEETING,
  LUOYE_LIVE_TRANSLATION,
} luoye_live_kind_t;

typedef struct {
  char client_session_id[LUOYE_LIVE_SESSION_ID_BYTES];
  char server_session_id[LUOYE_LIVE_SESSION_ID_BYTES];
  uint32_t revision;
  uint32_t display_revision;
  uint32_t caption_revision;
  uint32_t speaker_revision;
  uint32_t translation_revision;
  uint32_t summary_revision;
  bool revision_channels_supported;
  /* Local content generations. Unlike the server's session-wide revision,
     these advance only when pixels in the matching caption pane can change. */
  uint32_t caption_generation;
  uint32_t partial_generation;
  uint32_t contiguous_pcm_bytes;
  luoye_live_kind_t kind;
  bool final;
  bool failed;
  char meeting_text[LUOYE_LIVE_MEETING_BYTES];
  bool partial_supported;
  bool partial_active;
  char partial_text[LUOYE_LIVE_PARTIAL_BYTES];
  char source_text[LUOYE_LIVE_SOURCE_BYTES];
  char translated_text[LUOYE_LIVE_TRANSLATED_BYTES];
  char source_language[LUOYE_LIVE_LANGUAGE_BYTES];
  char target_language[LUOYE_LIVE_LANGUAGE_BYTES];
  bool timeline_available;
  uint16_t chapter_no;
  uint32_t chapter_start_ms;
  uint8_t chapter_mark_count;
  char chapter_title[LUOYE_LIVE_CHAPTER_TITLE_BYTES];
  char chapter_item_1[LUOYE_LIVE_CHAPTER_ITEM_BYTES];
  char chapter_item_2[LUOYE_LIVE_CHAPTER_ITEM_BYTES];
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

/* Applies caption resources as idempotent upserts. A speaker/metadata update
   for an existing seg_id never appends or reorders the displayed transcript. */
void luoye_live_caption_cache_init(luoye_live_caption_cache_t *cache);
bool luoye_live_caption_upsert(luoye_live_caption_cache_t *cache,
                               const char *seg_id, const char *text);
bool luoye_live_caption_build(const luoye_live_caption_cache_t *cache,
                              char *destination, size_t destination_size);

/* Builds the GET suffix without allowing an unbounded query string. */
bool luoye_live_query(char *out, size_t out_size, uint32_t after_revision,
                      uint32_t after_display_revision,
                      uint32_t after_caption_revision,
                      uint32_t after_speaker_revision,
                      uint32_t after_translation_revision,
                      uint32_t after_summary_revision);
