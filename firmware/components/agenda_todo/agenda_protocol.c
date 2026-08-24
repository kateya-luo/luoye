#include "agenda_protocol.h"

#include <stdio.h>
#include <string.h>

static bool safe_utf8(const unsigned char *s, size_t length) {
  size_t i = 0;
  while (i < length) {
    unsigned char b0 = s[i];
    if (b0 < 0x20 || b0 == 0x7f) return false;
    if (b0 < 0x80) { i++; continue; }
    if (b0 >= 0xc2 && b0 <= 0xdf) {
      if (i + 1 >= length || (s[i + 1] & 0xc0) != 0x80) return false;
      i += 2;
      continue;
    }
    if (b0 >= 0xe0 && b0 <= 0xef) {
      if (i + 2 >= length) return false;
      unsigned char b1 = s[i + 1], b2 = s[i + 2];
      if ((b1 & 0xc0) != 0x80 || (b2 & 0xc0) != 0x80 ||
          (b0 == 0xe0 && b1 < 0xa0) || (b0 == 0xed && b1 >= 0xa0)) {
        return false;
      }
      i += 3;
      continue;
    }
    /* Current 16-bit font index intentionally rejects supplementary planes. */
    return false;
  }
  return true;
}

bool luoye_agenda_text(char *destination, size_t destination_size,
                       const char *source) {
  if (!destination || destination_size == 0 || !source) return false;
  size_t length = strlen(source);
  if (length >= destination_size ||
      !safe_utf8((const unsigned char *)source, length)) return false;
  memcpy(destination, source, length + 1U);
  return true;
}

bool luoye_agenda_accept(uint32_t current_revision,
                         uint32_t current_binding_generation,
                         uint32_t incoming_revision,
                         uint32_t incoming_binding_generation) {
  if (incoming_binding_generation == 0) return false;
  if (current_binding_generation != 0 &&
      current_binding_generation != incoming_binding_generation) {
    return incoming_revision > 0;
  }
  return incoming_revision > current_revision;
}

int luoye_agenda_next_index(const luoye_agenda_snapshot_t *snapshot,
                            int64_t now_utc) {
  if (!snapshot) return -1;
  int found = -1;
  int64_t nearest = INT64_MAX;
  for (uint8_t i = 0; i < snapshot->count; i++) {
    const luoye_agenda_item_t *item = &snapshot->items[i];
    if (item->dismissed || item->reminder_utc <= now_utc) continue;
    if (item->reminder_utc < nearest) {
      nearest = item->reminder_utc;
      found = i;
    }
  }
  return found;
}

int luoye_agenda_due_index(const luoye_agenda_snapshot_t *snapshot,
                           int64_t now_utc, int64_t grace_seconds) {
  if (!snapshot || grace_seconds < 0) return -1;
  int found = -1;
  int64_t nearest = INT64_MAX;
  for (uint8_t i = 0; i < snapshot->count; i++) {
    const luoye_agenda_item_t *item = &snapshot->items[i];
    if (item->dismissed || item->reminder_utc <= 0 ||
        item->reminder_utc > now_utc + grace_seconds) continue;
    if (item->reminder_utc < nearest) {
      nearest = item->reminder_utc;
      found = i;
    }
  }
  return found;
}

bool luoye_agenda_query(char *out, size_t out_size, uint32_t after_revision) {
  if (!out || out_size == 0) return false;
  int length = snprintf(out, out_size, "?after_revision=%lu",
                        (unsigned long)after_revision);
  return length > 0 && (size_t)length < out_size;
}
