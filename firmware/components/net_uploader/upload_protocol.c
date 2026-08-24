#include "upload_protocol.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#define RETRY_BASE_MS 3000U
#define RETRY_MAX_MS  300000U

bool luoye_upload_plan_chunk(uint32_t total_bytes,
                             uint32_t acknowledged_bytes,
                             uint32_t next_seq,
                             uint32_t chunk_bytes,
                             luoye_upload_chunk_t *out) {
  if (!out || chunk_bytes == 0 || acknowledged_bytes > total_bytes ||
      acknowledged_bytes == total_bytes) {
    return false;
  }
  uint32_t pending = total_bytes - acknowledged_bytes;
  out->seq = next_seq;
  out->offset = acknowledged_bytes;
  out->length = pending < chunk_bytes ? pending : chunk_bytes;
  return true;
}

bool luoye_upload_ack_valid(const luoye_upload_chunk_t *chunk,
                            uint32_t server_next_seq,
                            uint32_t server_acknowledged_bytes) {
  return chunk &&
         server_next_seq == chunk->seq + 1U &&
         server_acknowledged_bytes == chunk->offset + chunk->length;
}

bool luoye_upload_ack_progress_valid(const luoye_upload_chunk_t *chunk,
                                     uint32_t total_bytes,
                                     uint32_t chunk_bytes,
                                     uint32_t server_next_seq,
                                     uint32_t server_acknowledged_bytes) {
  if (!chunk || chunk_bytes == 0 || (server_acknowledged_bytes & 1U) ||
      server_acknowledged_bytes > total_bytes ||
      server_acknowledged_bytes < chunk->offset + chunk->length ||
      server_next_seq <= chunk->seq) return false;
  uint32_t expected_seq = server_acknowledged_bytes / chunk_bytes;
  if (server_acknowledged_bytes % chunk_bytes) expected_seq++;
  return server_next_seq == expected_seq;
}

bool luoye_upload_progress_from_samples(uint32_t total_bytes,
                                        uint32_t chunk_bytes,
                                        uint32_t server_next_seq,
                                        uint32_t server_received_samples,
                                        uint32_t *acknowledged_bytes) {
  if (!acknowledged_bytes || chunk_bytes == 0 ||
      server_received_samples > UINT32_MAX / 2U) return false;
  uint32_t bytes = server_received_samples * 2U;
  if (bytes > total_bytes) return false;
  uint32_t expected_seq = bytes / chunk_bytes;
  if (bytes % chunk_bytes) expected_seq++;
  if (server_next_seq != expected_seq) return false;
  *acknowledged_bytes = bytes;
  return true;
}

luoye_upload_http_class_t luoye_upload_classify_http(bool transport_ok,
                                                      int http_status) {
  if (!transport_ok || http_status <= 0 || http_status == 408 ||
      http_status == 425 || http_status == 429 || http_status >= 500) {
    return LUOYE_UPLOAD_HTTP_RETRY;
  }
  if (http_status >= 200 && http_status < 300) return LUOYE_UPLOAD_HTTP_OK;
  if (http_status == 401 || http_status == 403) return LUOYE_UPLOAD_HTTP_AUTH;
  if (http_status == 409) return LUOYE_UPLOAD_HTTP_CONFLICT;
  return LUOYE_UPLOAD_HTTP_PERMANENT;
}

uint32_t luoye_upload_retry_delay_ms(uint32_t retry_count,
                                     uint32_t random_value) {
  uint32_t shift = retry_count > 6U ? 6U : retry_count;
  uint32_t base = RETRY_BASE_MS << shift;
  if (base > RETRY_MAX_MS) base = RETRY_MAX_MS;
  uint32_t room = RETRY_MAX_MS - base;
  uint32_t jitter_cap = base / 4U;
  if (jitter_cap > room) jitter_cap = room;
  return base + (jitter_cap ? random_value % (jitter_cap + 1U) : 0U);
}

bool luoye_upload_create_key(char *out, size_t out_size,
                             const char *session_id) {
  if (!out || !out_size || !session_id) return false;
  int count = snprintf(out, out_size, "session:%s:create", session_id);
  return count > 0 && (size_t)count < out_size;
}

bool luoye_upload_chunk_key(char *out, size_t out_size,
                            const char *session_id, uint32_t seq,
                            const char sha256_hex[65]) {
  if (!out || !out_size || !session_id || !sha256_hex) return false;
  int count = snprintf(out, out_size, "session:%s:audio:%lu:%s",
                       session_id, (unsigned long)seq, sha256_hex);
  return count > 0 && (size_t)count < out_size;
}

bool luoye_upload_mark_key(char *out, size_t out_size,
                           const char *session_id, const char *mark_id) {
  if (!out || !out_size || !session_id || !mark_id) return false;
  int count = snprintf(out, out_size, "session:%s:mark:%s",
                       session_id, mark_id);
  return count > 0 && (size_t)count < out_size;
}

bool luoye_upload_final_key(char *out, size_t out_size,
                            const char *session_id) {
  if (!out || !out_size || !session_id) return false;
  int count = snprintf(out, out_size, "session:%s:end", session_id);
  return count > 0 && (size_t)count < out_size;
}

bool luoye_upload_safe_path_id(const char *value) {
  if (!value || !*value) return false;
  for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
    if (!isalnum(*p) && *p != '-' && *p != '_' && *p != '.') return false;
  }
  return true;
}

luoye_upload_mark_read_t luoye_upload_read_mark_line(
    FILE *stream, char *buffer, size_t capacity,
    uint32_t *line_number, size_t *line_length) {
  if (!stream || !buffer || capacity < 2 || !line_number || !line_length ||
      *line_number == UINT32_MAX) {
    return LUOYE_UPLOAD_MARK_IO_ERROR;
  }
  buffer[0] = '\0';
  *line_length = 0;
  if (!fgets(buffer, (int)capacity, stream)) {
    return ferror(stream) ? LUOYE_UPLOAD_MARK_IO_ERROR
                          : LUOYE_UPLOAD_MARK_EOF;
  }
  (*line_number)++;
  size_t length = strlen(buffer);
  bool newline = length > 0 && buffer[length - 1] == '\n';
  if (!newline) {
    if (!feof(stream)) {
      int value;
      do {
        value = fgetc(stream);
      } while (value != '\n' && value != EOF);
      if (ferror(stream)) return LUOYE_UPLOAD_MARK_IO_ERROR;
    }
    buffer[0] = '\0';
    return LUOYE_UPLOAD_MARK_SKIPPED;
  }
  buffer[--length] = '\0';
  if (length && buffer[length - 1] == '\r') buffer[--length] = '\0';
  if (!length) return LUOYE_UPLOAD_MARK_SKIPPED;
  *line_length = length;
  return LUOYE_UPLOAD_MARK_READY;
}
