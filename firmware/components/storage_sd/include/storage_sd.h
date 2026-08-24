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

/* Destructive, user-confirmed initialization for the luoye-storage/2 layout.
 * The caller must reboot after ESP_OK so every storage consumer starts from a
 * freshly mounted volume. */
esp_err_t storage_sd_format(void);

/*
 * Serialize card reads and apply DMA low-water backpressure. ESP-IDF's
 * original SDSPI transaction lengths are preserved; arbitrary offsets are
 * handled by FatFs and the stock driver without changing bytes on the wire.
 */
esp_err_t storage_sd_read(FILE *file, void *buffer, size_t wanted,
                          size_t *received);

/* Positioned read helpers use the descriptor API exclusively. Do not mix
 * stdio fseek/ftell state with storage_sd_read(), which calls POSIX read(). */
esp_err_t storage_sd_seek(FILE *file, uint32_t offset);
esp_err_t storage_sd_size(FILE *file, size_t *size_out);

/*
 * Prepare a WAV range before opening HTTP. The one-byte read materializes the
 * FatFs sector cache and validates the card while network memory is quiet.
 */
esp_err_t storage_sd_prepare_range(FILE *file, uint32_t file_offset);

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
