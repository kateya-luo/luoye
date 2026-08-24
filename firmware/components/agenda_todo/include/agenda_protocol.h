#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LUOYE_AGENDA_MAX_ITEMS       24
#define LUOYE_AGENDA_ID_BYTES        48
#define LUOYE_AGENDA_TITLE_BYTES     72
#define LUOYE_AGENDA_TIME_BYTES      24
#define LUOYE_AGENDA_TIMEZONE_BYTES  32

typedef struct {
  char id[LUOYE_AGENDA_ID_BYTES];
  char title[LUOYE_AGENDA_TITLE_BYTES];
  char display_time[LUOYE_AGENDA_TIME_BYTES];
  int64_t start_utc;
  int64_t reminder_utc;
  bool has_time;
  bool dismissed;
} luoye_agenda_item_t;

typedef struct {
  uint32_t revision;
  uint32_t binding_generation;
  int32_t timezone_offset_minutes;
  int64_t synced_utc;
  char timezone[LUOYE_AGENDA_TIMEZONE_BYTES];
  uint8_t count;
  luoye_agenda_item_t items[LUOYE_AGENDA_MAX_ITEMS];
} luoye_agenda_snapshot_t;

/* Strict bounded UTF-8 used for all server-controlled EPD text. */
bool luoye_agenda_text(char *destination, size_t destination_size,
                       const char *source);

/* Revision and account generation must advance without crossing bindings. */
bool luoye_agenda_accept(uint32_t current_revision,
                         uint32_t current_binding_generation,
                         uint32_t incoming_revision,
                         uint32_t incoming_binding_generation);

/* Finds the nearest non-dismissed reminder strictly after 'now_utc'. */
int luoye_agenda_next_index(const luoye_agenda_snapshot_t *snapshot,
                            int64_t now_utc);

/* Finds a reminder due now, allowing the minute-granularity RTC grace window. */
int luoye_agenda_due_index(const luoye_agenda_snapshot_t *snapshot,
                           int64_t now_utc, int64_t grace_seconds);

bool luoye_agenda_query(char *out, size_t out_size, uint32_t after_revision);
