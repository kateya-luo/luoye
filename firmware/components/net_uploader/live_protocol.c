#include "live_protocol.h"

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
    /* The current 16-bit font index cannot render supplementary planes. */
    return false;
  }
  return true;
}

bool luoye_live_cursor_accept(uint32_t current_revision,
                              uint32_t current_contiguous_pcm_bytes,
                              uint32_t acknowledged_pcm_bytes,
                              uint32_t incoming_revision,
                              uint32_t incoming_contiguous_pcm_bytes) {
  return incoming_revision > current_revision &&
         incoming_contiguous_pcm_bytes >= current_contiguous_pcm_bytes &&
         incoming_contiguous_pcm_bytes <= acknowledged_pcm_bytes;
}

bool luoye_live_set_text(char *destination, size_t destination_size,
                         const char *source) {
  if (!destination || destination_size == 0 || !source) return false;
  size_t length = strlen(source);
  if (length >= destination_size ||
      !safe_utf8((const unsigned char *)source, length)) return false;
  memcpy(destination, source, length + 1U);
  return true;
}

static size_t next_utf8(const char *text, size_t length, size_t offset) {
  if (offset >= length) return length;
  offset++;
  while (offset < length &&
         (((unsigned char)text[offset] & 0xc0U) == 0x80U)) offset++;
  return offset;
}

static size_t utf8_suffix_start(const char *text, size_t length,
                                size_t maximum_bytes) {
  if (length <= maximum_bytes) return 0;
  size_t start = length - maximum_bytes;
  while (start < length &&
         (((unsigned char)text[start] & 0xc0U) == 0x80U)) start++;
  return start;
}

bool luoye_live_append_text(char *destination, size_t destination_size,
                            const char *source) {
  if (!destination || destination_size == 0 || !source) return false;
  size_t destination_length = strnlen(destination, destination_size);
  size_t source_length = strlen(source);
  if (destination_length >= destination_size ||
      !safe_utf8((const unsigned char *)destination, destination_length) ||
      !safe_utf8((const unsigned char *)source, source_length)) return false;
  if (source_length == 0) return true;

  size_t capacity = destination_size - 1U;
  size_t source_start = utf8_suffix_start(source, source_length, capacity);
  source += source_start;
  source_length -= source_start;
  if (source_start) {
    destination[0] = '\0';
    destination_length = 0;
  }

  size_t separator = destination_length ? 1U : 0U;
  while (destination_length + separator + source_length > capacity &&
         destination_length) {
    const char *unit_end = strchr(destination, ' ');
    size_t drop = unit_end
                    ? (size_t)(unit_end - destination) + 1U
                    : next_utf8(destination, destination_length, 0);
    memmove(destination, destination + drop, destination_length - drop + 1U);
    destination_length -= drop;
    while (destination_length && destination[0] == ' ') {
      memmove(destination, destination + 1, destination_length);
      destination_length--;
    }
    separator = destination_length ? 1U : 0U;
  }
  if (separator) destination[destination_length++] = ' ';
  memcpy(destination + destination_length, source, source_length + 1U);
  return true;
}

bool luoye_live_query(char *out, size_t out_size, uint32_t after_revision) {
  if (!out || out_size == 0) return false;
  int length = snprintf(out, out_size, "?after_revision=%lu",
                        (unsigned long)after_revision);
  return length > 0 && (size_t)length < out_size;
}
