#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "esp_err.h"

#define SD_UPLOAD_SESSION_ID_BYTES 48
#define SD_UPLOAD_SERVER_ID_BYTES  72
#define SD_UPLOAD_DEVICE_ID_BYTES  40
#define SD_UPLOAD_DIR_BYTES        160
#define SD_STORAGE_PAGE_MAX         12
#define SD_UPLOAD_RANGE_BLOCK_BYTES (10U * 1024U * 1024U)
#define SD_UPLOAD_SHA256_HEX_BYTES   65
#define SD_UPLOAD_RANGE_HASH_FILE    "audio.sha256"

typedef struct {
  char session_id[SD_UPLOAD_SESSION_ID_BYTES];
  char server_session_id[SD_UPLOAD_SERVER_ID_BYTES];
  char device_id[SD_UPLOAD_DEVICE_ID_BYTES];
  char directory[SD_UPLOAD_DIR_BYTES];
  char scene[16];
  char title[48];
  char state[24];
  /* live = per-session realtime chunks; bulk/repair = API/2 byte ranges. */
  char upload_mode[8];
  uint32_t binding_generation;
  uint32_t next_seq;
  uint32_t live_chunk_bytes;
  uint32_t acknowledged_bytes;
  uint32_t gap_start_bytes;
  uint32_t pcm_bytes;
  uint32_t retry_count;
  uint32_t result_revision;
  uint32_t display_revision;
  uint32_t caption_revision;
  uint32_t speaker_revision;
  uint32_t translation_revision;
  uint32_t summary_revision;
  uint32_t result_pcm_bytes;
  int64_t started_at_utc;
  int64_t ended_at_utc;
  int last_http_status;
  bool local_closed;
  bool remote_session_created;
  bool marks_acked;
  bool final_acked;
  bool live_resume_required;
  bool deferred_gaps;
  bool defer_acked;
} sd_upload_item_t;

/* One sequential reader is retained for the active live session.  Keeping the
 * descriptor open avoids an O(file-size) FAT-chain walk for every one-second
 * chunk when FastSeek is unavailable.  The reader never owns the recorder's
 * write handle and must be closed before local deletion or range repair. */
typedef struct {
  FILE *file;
  char session_id[SD_UPLOAD_SESSION_ID_BYTES];
  uint32_t file_offset;
} sd_upload_reader_t;

typedef struct {
  char session_id[SD_UPLOAD_SESSION_ID_BYTES];
  char server_session_id[SD_UPLOAD_SERVER_ID_BYTES];
  char state[24];
  uint64_t local_bytes;
  int64_t ended_at_utc;
  bool deletable;
} sd_storage_session_t;

esp_err_t sd_upload_store_init(void);
esp_err_t sd_upload_assign_identity(const char *session_id,
                                    const char *device_id,
                                    uint32_t binding_generation);
esp_err_t sd_upload_find(uint32_t binding_generation,
                         const char *session_id,
                         sd_upload_item_t *out);
esp_err_t sd_upload_next(uint32_t binding_generation, sd_upload_item_t *out);
/* Returns the active recording at its last fsync-confirmed PCM boundary. */
esp_err_t sd_upload_current(uint32_t binding_generation, sd_upload_item_t *out);
/* Refresh only the recorder-owned durable watermark and close metadata of an
 * already loaded active item.  Upload cursors held in RAM are not overwritten
 * by an older upload.state checkpoint. */
esp_err_t sd_upload_refresh_current(sd_upload_item_t *item);
esp_err_t sd_upload_backlog(uint32_t *session_count, uint64_t *pending_bytes);
esp_err_t sd_upload_read_audio(const sd_upload_item_t *item,
                               uint32_t offset, void *buffer,
                               size_t wanted, size_t *received);
esp_err_t sd_upload_reader_read(sd_upload_reader_t *reader,
                                const sd_upload_item_t *item,
                                uint32_t offset, void *buffer,
                                size_t wanted, size_t *received);
void sd_upload_reader_close(sd_upload_reader_t *reader);
/* Returns a recorder-generated digest for an exact durable 10 MiB range (or
 * the final short range). Older/recovered sessions may not have this index;
 * callers must fall back to scanning audio.wav on ESP_ERR_NOT_FOUND. */
esp_err_t sd_upload_range_sha256(const sd_upload_item_t *item,
                                 uint32_t offset, uint32_t length,
                                 char sha256[SD_UPLOAD_SHA256_HEX_BYTES]);
esp_err_t sd_upload_read_marks(const sd_upload_item_t *item,
                               void *buffer, size_t capacity,
                               size_t *received);
esp_err_t sd_upload_save(sd_upload_item_t *item);

/* Fixed-SD inventory. Any closed local session from the current binding may
 * be explicitly removed by its owner, independently of cloud meeting state. */
esp_err_t sd_storage_info(uint64_t *total_bytes, uint64_t *free_bytes);
esp_err_t sd_storage_inventory_page(uint32_t binding_generation,
                                    const char *after_session_id,
                                    sd_storage_session_t *items,
                                    size_t capacity, size_t *count,
                                    char *next_cursor, size_t next_cursor_size,
                                    bool *complete);
esp_err_t sd_storage_delete_local(uint32_t binding_generation,
                                  const char *session_id,
                                  uint64_t *freed_bytes);
/* Removes every local session from the current binding.  A currently open
 * recording is never touched; ESP_ERR_INVALID_STATE asks the cloud command
 * dispatcher to keep the command pending until that WAV is safely closed. */
esp_err_t sd_storage_delete_all_local(uint32_t binding_generation,
                                      uint32_t *deleted_count,
                                      uint64_t *freed_bytes);
