#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef enum {
  LUOYE_UPLOAD_HTTP_OK = 0,
  LUOYE_UPLOAD_HTTP_RETRY,
  LUOYE_UPLOAD_HTTP_AUTH,
  LUOYE_UPLOAD_HTTP_CONFLICT,
  LUOYE_UPLOAD_HTTP_PERMANENT,
} luoye_upload_http_class_t;

typedef struct {
  uint32_t seq;
  uint32_t offset;
  uint32_t length;
} luoye_upload_chunk_t;

typedef enum {
  LUOYE_UPLOAD_MARK_IO_ERROR = -1,
  LUOYE_UPLOAD_MARK_EOF = 0,
  LUOYE_UPLOAD_MARK_READY,
  LUOYE_UPLOAD_MARK_SKIPPED,
} luoye_upload_mark_read_t;

bool luoye_upload_plan_chunk(uint32_t total_bytes,
                             uint32_t acknowledged_bytes,
                             uint32_t next_seq,
                             uint32_t chunk_bytes,
                             luoye_upload_chunk_t *out);
bool luoye_upload_ack_valid(const luoye_upload_chunk_t *chunk,
                            uint32_t server_next_seq,
                            uint32_t server_acknowledged_bytes);
bool luoye_upload_ack_progress_valid(const luoye_upload_chunk_t *chunk,
                                     uint32_t total_bytes,
                                     uint32_t chunk_bytes,
                                     uint32_t server_next_seq,
                                     uint32_t server_acknowledged_bytes);
bool luoye_upload_progress_from_samples(uint32_t total_bytes,
                                        uint32_t chunk_bytes,
                                        uint32_t server_next_seq,
                                        uint32_t server_received_samples,
                                        uint32_t *acknowledged_bytes);
luoye_upload_http_class_t luoye_upload_classify_http(bool transport_ok,
                                                      int http_status);
uint32_t luoye_upload_retry_delay_ms(uint32_t retry_count,
                                     uint32_t random_value);
bool luoye_upload_create_key(char *out, size_t out_size,
                             const char *session_id);
bool luoye_upload_chunk_key(char *out, size_t out_size,
                            const char *session_id, uint32_t seq,
                            const char sha256_hex[65]);
bool luoye_upload_mark_key(char *out, size_t out_size,
                           const char *session_id, const char *mark_id);
bool luoye_upload_final_key(char *out, size_t out_size,
                            const char *session_id);
bool luoye_upload_safe_path_id(const char *value);

// Reads one physical JSONL record without imposing a limit on total file
// size.  A torn/empty/overlong record is consumed and reported as SKIPPED;
// callers can continue with the next stable line number.
luoye_upload_mark_read_t luoye_upload_read_mark_line(
    FILE *stream, char *buffer, size_t capacity,
    uint32_t *line_number, size_t *line_length);
