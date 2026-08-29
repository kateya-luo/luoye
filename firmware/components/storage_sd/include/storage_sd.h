#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "app_state.h"
#include "esp_err.h"

typedef void (*storage_post_fn)(app_event_t event, int32_t arg);

esp_err_t storage_sd_init(storage_post_fn post);
bool storage_sd_mounted(void);
bool storage_sd_faulted(void);

/* Promote a confirmed filesystem/device I/O failure to the single runtime
 * FAULTED state.  The first caller logs the transition; later callers become
 * no-ops so a broken SDSPI command stream cannot create an error storm. */
void storage_sd_report_io_fault(const char *source, esp_err_t error,
                                int io_errno);

/*
 * Read through a permanently reserved internal/DMA-capable staging buffer.
 * Large upload buffers live in PSRAM; passing them directly to SDSPI makes the
 * IDF allocate a temporary internal RX buffer for every transaction.  A failed
 * allocation enters a broken cleanup path in ESP-IDF 5.5.4, so all potentially
 * external destinations must use this helper.
 */
esp_err_t storage_sd_read(FILE *file, void *buffer, size_t wanted,
                          size_t *received);

/* Persistent counter + MAC + random suffix; prevents reset-time name collisions. */
esp_err_t sd_session_generate_id(char *out, size_t out_size);

/* The open call returns only after all four session files are durable. */
app_error_t sd_session_open(const char *session_id,
                            app_scene_t scene,
                            const char *title);
void sd_session_request_close(app_close_reason_t reason);
bool sd_session_is_open(void);
/* APP_ERR_BUSY while draining, APP_ERR_NONE after a durable close, otherwise
 * the terminal storage error. */
app_error_t sd_session_close_status(void);
esp_err_t sd_session_mark(const char *kind, int64_t at_ms);

/*
 * data_bytes is the last fsync-confirmed PCM boundary while recording and the
 * final PCM length after close. The uploader must never read beyond it.
 */
bool sd_session_current(char *dir_out, size_t dir_len,
                        char *id_out, size_t id_len,
                        bool *closed, uint32_t *data_bytes);
