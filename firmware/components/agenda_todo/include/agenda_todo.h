#pragma once

#include "agenda_protocol.h"
#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LUOYE_TODO_ID_BYTES       48
#define LUOYE_TODO_SERVER_BYTES   72
#define LUOYE_TODO_TEXT_BYTES     128
#define LUOYE_TODO_PATH_BYTES     192

typedef enum {
  LUOYE_TODO_NONE = 0,
  LUOYE_TODO_CAPTURING,
  LUOYE_TODO_QUEUED,
  LUOYE_TODO_UPLOADED,
  LUOYE_TODO_NEEDS_CONFIRMATION,
  LUOYE_TODO_CONFIRM_PENDING,
  LUOYE_TODO_CANCEL_PENDING,
  LUOYE_TODO_CREATED,
  LUOYE_TODO_CANCELLED,
  LUOYE_TODO_FAILED,
} luoye_todo_state_t;

typedef struct {
  char id[LUOYE_TODO_ID_BYTES];
  char directory[LUOYE_TODO_PATH_BYTES];
  char server_id[LUOYE_TODO_SERVER_BYTES];
  uint32_t binding_generation;
  uint32_t pcm_bytes;
  uint32_t result_revision;
  luoye_todo_state_t state;
  int last_http_status;
  uint32_t retry_count;
  int64_t created_utc;
  int64_t due_utc;
  char transcript[LUOYE_TODO_TEXT_BYTES];
  char title[LUOYE_AGENDA_TITLE_BYTES];
  char display_time[LUOYE_AGENDA_TIME_BYTES];
} luoye_todo_item_t;

typedef enum {
  LUOYE_REMINDER_DISMISS = 0,
  LUOYE_REMINDER_SNOOZE,
  LUOYE_REMINDER_START_MEETING,
} luoye_reminder_action_t;

esp_err_t agenda_todo_init(void);

bool agenda_snapshot_get(luoye_agenda_snapshot_t *out);
esp_err_t agenda_apply_server_json(const char *json,
                                   uint32_t expected_binding_generation);
void agenda_reset_binding(uint32_t binding_generation);
esp_err_t agenda_schedule_next(void);
bool agenda_take_due(int64_t now_utc, luoye_agenda_item_t *out);
esp_err_t agenda_reminder_action(luoye_reminder_action_t action,
                                 int snooze_minutes);

esp_err_t todo_generate_id(char *out, size_t out_size);
esp_err_t todo_capture_begin(const char *todo_id,
                             uint32_t binding_generation);
esp_err_t todo_capture_end(bool save, luoye_todo_item_t *out);
bool todo_capture_active(void);
bool todo_latest(luoye_todo_item_t *out);
esp_err_t todo_next(uint32_t binding_generation, luoye_todo_item_t *out);
esp_err_t todo_read_audio(const luoye_todo_item_t *item,
                          uint8_t *buffer, size_t capacity, size_t *size_out);
esp_err_t todo_save(luoye_todo_item_t *item);
esp_err_t todo_set_result(const char *todo_id, const char *server_id,
                          uint32_t revision, const char *transcript,
                          const char *title, int64_t due_utc,
                          const char *display_time, bool needs_confirmation);
esp_err_t todo_request_action(uint32_t binding_generation, bool confirm);
