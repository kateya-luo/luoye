#include "agenda_todo.h"

#include "audio_pdm.h"
#include "power_mgr.h"
#include "storage_format.h"
#include "storage_sd.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

#define LUOYE_ROOT        "/sdcard/luoye"
#define AGENDA_PATH       LUOYE_ROOT "/agenda.json"
#define TODO_ROOT         "/sdcard/todo"
#define JSON_LIMIT        (64U * 1024U)
#define TODO_RING_BYTES   (64U * 1024U)
#define TODO_MAX_PCM      (30U * AUDIO_SAMPLE_RATE * sizeof(int16_t))
#define TODO_WRITE_BYTES  4096U
#define TODO_SYNC_BYTES   (64U * 1024U)
#define PATH_BYTES        256

static const char *TAG = "agenda";
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_todo_done;
static StreamBufferHandle_t s_todo_ring;
static luoye_agenda_snapshot_t s_agenda;
static bool s_have_agenda;
static char s_active_reminder[LUOYE_AGENDA_ID_BYTES];
static luoye_todo_item_t s_latest_todo;

static struct {
  FILE *wav;
  luoye_todo_item_t item;
  volatile bool active;
  volatile bool closing;
  volatile bool keep;
  volatile bool write_error;
  uint32_t synced_pcm_bytes;
} s_capture;

static bool make_dir(const char *path) {
  return mkdir(path, 0775) == 0 || errno == EEXIST;
}

static bool todo_id_valid(const char *id) {
  if (!id || !id[0]) return false;
  size_t length = strnlen(id, LUOYE_TODO_ID_BYTES);
  if (length == LUOYE_TODO_ID_BYTES) return false;
  for (size_t i = 0; i < length; i++) {
    char c = id[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.')) return false;
  }
  return true;
}

static bool todo_directory(const char *id, char *out, size_t out_size) {
  if (!todo_id_valid(id) || !out) return false;
  size_t root_length = strlen(TODO_ROOT);
  size_t id_length = strlen(id);
  if (root_length + 1U + id_length + 1U > out_size) return false;
  memcpy(out, TODO_ROOT, root_length);
  out[root_length] = '/';
  memcpy(out + root_length + 1U, id, id_length + 1U);
  return true;
}

static bool json_number_i64(cJSON *root, const char *name, int64_t *out) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  if (!cJSON_IsNumber(value) || value->valuedouble < 0 ||
      value->valuedouble > 9007199254740991.0) return false;
  int64_t converted = (int64_t)value->valuedouble;
  if ((double)converted != value->valuedouble) return false;
  *out = converted;
  return true;
}

static bool json_number_u32(cJSON *root, const char *name, uint32_t *out) {
  int64_t value = 0;
  if (!json_number_i64(root, name, &value) || value > UINT32_MAX) return false;
  *out = (uint32_t)value;
  return true;
}

static bool sync_text(const char *path, const char *text) {
  FILE *file = fopen(path, "wb");
  if (!file) return false;
  size_t bytes = strlen(text);
  bool ok = fwrite(text, 1, bytes, file) == bytes && fflush(file) == 0;
  int fd = fileno(file);
  if (ok && (fd < 0 || fsync(fd) != 0)) ok = false;
  if (fclose(file) != 0) ok = false;
  return ok;
}

static esp_err_t write_json_atomic(const char *path, cJSON *root) {
  char *json = cJSON_PrintUnformatted(root);
  if (!json) return ESP_ERR_NO_MEM;
  char temp[PATH_BYTES], backup[PATH_BYTES];
  if (snprintf(temp, sizeof(temp), "%s.tmp", path) >= (int)sizeof(temp) ||
      snprintf(backup, sizeof(backup), "%s.bak", path) >= (int)sizeof(backup)) {
    cJSON_free(json);
    return ESP_ERR_INVALID_SIZE;
  }
  bool ok = sync_text(temp, json);
  cJSON_free(json);
  if (!ok) { unlink(temp); return ESP_FAIL; }
  unlink(backup);
  bool had_current = access(path, F_OK) == 0;
  if (had_current && rename(path, backup) != 0) {
    unlink(temp);
    return ESP_FAIL;
  }
  if (rename(temp, path) != 0) {
    if (had_current) rename(backup, path);
    unlink(temp);
    return ESP_FAIL;
  }
  return ESP_OK;
}

static cJSON *read_json_one(const char *path) {
  FILE *file = fopen(path, "rb");
  if (!file) return NULL;
  size_t length = 0;
  if (storage_sd_size(file, &length) != ESP_OK || length > JSON_LIMIT ||
      storage_sd_seek(file, 0) != ESP_OK) {
    fclose(file);
    return NULL;
  }
  char *data = malloc(length + 1U);
  if (!data) { fclose(file); return NULL; }
  size_t got = 0;
  esp_err_t read_result = storage_sd_read(file, data, length, &got);
  fclose(file);
  if (read_result != ESP_OK || got != length) {
    free(data);
    return NULL;
  }
  data[length] = '\0';
  cJSON *root = cJSON_Parse(data);
  free(data);
  return root;
}

static cJSON *agenda_to_json(const luoye_agenda_snapshot_t *snapshot) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return NULL;
  cJSON_AddStringToObject(root, "schema", "luoye-agenda/1");
  cJSON_AddNumberToObject(root, "revision", snapshot->revision);
  cJSON_AddNumberToObject(root, "binding_generation", snapshot->binding_generation);
  cJSON_AddNumberToObject(root, "timezone_offset_minutes",
                          snapshot->timezone_offset_minutes);
  cJSON_AddNumberToObject(root, "synced_utc", (double)snapshot->synced_utc);
  cJSON_AddStringToObject(root, "timezone", snapshot->timezone);
  cJSON *items = cJSON_AddArrayToObject(root, "items");
  if (!items) { cJSON_Delete(root); return NULL; }
  for (uint8_t i = 0; i < snapshot->count; i++) {
    const luoye_agenda_item_t *item = &snapshot->items[i];
    cJSON *entry = cJSON_CreateObject();
    if (!entry) { cJSON_Delete(root); return NULL; }
    cJSON_AddStringToObject(entry, "id", item->id);
    cJSON_AddStringToObject(entry, "title", item->title);
    cJSON_AddStringToObject(entry, "display_time", item->display_time);
    cJSON_AddNumberToObject(entry, "start_utc", (double)item->start_utc);
    cJSON_AddNumberToObject(entry, "reminder_utc", (double)item->reminder_utc);
    cJSON_AddBoolToObject(entry, "has_time", item->has_time);
    cJSON_AddBoolToObject(entry, "dismissed", item->dismissed);
    cJSON_AddItemToArray(items, entry);
  }
  return root;
}

static esp_err_t agenda_save_locked(void) {
  cJSON *root = agenda_to_json(&s_agenda);
  if (!root) return ESP_ERR_NO_MEM;
  esp_err_t result = write_json_atomic(AGENDA_PATH, root);
  cJSON_Delete(root);
  return result;
}

static bool parse_agenda(cJSON *root, luoye_agenda_snapshot_t *out,
                         bool local_file) {
  memset(out, 0, sizeof(*out));
  uint32_t revision = 0, binding = 0;
  int64_t synced = 0;
  cJSON *offset = cJSON_GetObjectItemCaseSensitive(root,
                                                   "timezone_offset_minutes");
  cJSON *timezone = cJSON_GetObjectItemCaseSensitive(root, "timezone");
  cJSON *items = cJSON_GetObjectItemCaseSensitive(root, "items");
  if (!json_number_u32(root, "revision", &revision) ||
      !json_number_u32(root, "binding_generation", &binding) ||
      !json_number_i64(root, local_file ? "synced_utc" : "server_time_utc",
                       &synced) ||
      !cJSON_IsNumber(offset) || offset->valuedouble < -840 ||
      offset->valuedouble > 840 ||
      !cJSON_IsString(timezone) || !cJSON_IsArray(items) ||
      cJSON_GetArraySize(items) > LUOYE_AGENDA_MAX_ITEMS) return false;
  int32_t offset_minutes = (int32_t)offset->valuedouble;
  if ((double)offset_minutes != offset->valuedouble ||
      !luoye_agenda_text(out->timezone, sizeof(out->timezone),
                         timezone->valuestring)) return false;
  out->revision = revision;
  out->binding_generation = binding;
  out->timezone_offset_minutes = offset_minutes;
  out->synced_utc = synced;
  cJSON *entry;
  cJSON_ArrayForEach(entry, items) {
    cJSON *id = cJSON_GetObjectItemCaseSensitive(entry, "id");
    cJSON *title = cJSON_GetObjectItemCaseSensitive(entry, "title");
    cJSON *display = cJSON_GetObjectItemCaseSensitive(entry, "display_time");
    cJSON *has_time = cJSON_GetObjectItemCaseSensitive(entry, "has_time");
    luoye_agenda_item_t *item = &out->items[out->count];
    if (!cJSON_IsObject(entry) || !cJSON_IsString(id) ||
        !cJSON_IsString(title) || !cJSON_IsString(display) ||
        !json_number_i64(entry, "start_utc", &item->start_utc) ||
        !json_number_i64(entry, "reminder_utc", &item->reminder_utc) ||
        !todo_id_valid(id->valuestring) ||
        !luoye_agenda_text(item->title, sizeof(item->title), title->valuestring) ||
        !luoye_agenda_text(item->display_time, sizeof(item->display_time),
                           display->valuestring)) return false;
    snprintf(item->id, sizeof(item->id), "%s", id->valuestring);
    item->has_time = cJSON_IsBool(has_time) ? cJSON_IsTrue(has_time)
                                            : item->start_utc > 0;
    if ((item->has_time && (item->start_utc <= 0 ||
                            item->reminder_utc > item->start_utc)) ||
        (!item->has_time && (item->start_utc != 0 || item->reminder_utc != 0))) {
      return false;
    }
    item->dismissed = local_file &&
        cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(entry, "dismissed"));
    out->count++;
  }
  return binding > 0;
}

static void preserve_local_actions(luoye_agenda_snapshot_t *incoming) {
  if (!s_have_agenda || s_agenda.binding_generation != incoming->binding_generation) {
    return;
  }
  for (uint8_t i = 0; i < incoming->count; i++) {
    for (uint8_t j = 0; j < s_agenda.count; j++) {
      if (strcmp(incoming->items[i].id, s_agenda.items[j].id) == 0) {
        incoming->items[i].dismissed = s_agenda.items[j].dismissed;
        if (!s_agenda.items[j].dismissed &&
            s_agenda.items[j].reminder_utc > incoming->items[i].reminder_utc) {
          incoming->items[i].reminder_utc = s_agenda.items[j].reminder_utc;
        }
        break;
      }
    }
  }
}

bool agenda_snapshot_get(luoye_agenda_snapshot_t *out) {
  if (!s_lock || !out) return false;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  bool available = s_have_agenda;
  if (available) *out = s_agenda;
  xSemaphoreGive(s_lock);
  return available;
}

esp_err_t agenda_apply_server_json(const char *json,
                                   uint32_t expected_binding_generation) {
  if (!s_lock || !json || expected_binding_generation == 0) {
    return ESP_ERR_INVALID_ARG;
  }
  cJSON *root = cJSON_Parse(json);
  if (!root) return ESP_ERR_INVALID_RESPONSE;
  luoye_agenda_snapshot_t incoming;
  bool valid = parse_agenda(root, &incoming, false);
  cJSON_Delete(root);
  if (!valid || incoming.binding_generation != expected_binding_generation) {
    return ESP_ERR_INVALID_RESPONSE;
  }
  xSemaphoreTake(s_lock, portMAX_DELAY);
  if (!luoye_agenda_accept(s_have_agenda ? s_agenda.revision : 0,
                           s_have_agenda ? s_agenda.binding_generation : 0,
                           incoming.revision, incoming.binding_generation)) {
    xSemaphoreGive(s_lock);
    return ESP_ERR_INVALID_STATE;
  }
  preserve_local_actions(&incoming);
  s_agenda = incoming;
  s_have_agenda = true;
  esp_err_t result = agenda_save_locked();
  xSemaphoreGive(s_lock);
  if (result == ESP_OK) result = agenda_schedule_next();
  return result;
}

void agenda_reset_binding(uint32_t binding_generation) {
  if (!s_lock) return;
  bool binding_changed = false;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  if (s_have_agenda && s_agenda.binding_generation != binding_generation) {
    binding_changed = true;
    memset(&s_agenda, 0, sizeof(s_agenda));
    s_agenda.binding_generation = binding_generation;
    s_have_agenda = false;
    s_active_reminder[0] = '\0';
    unlink(AGENDA_PATH);
    char backup[PATH_BYTES];
    snprintf(backup, sizeof(backup), "%s.bak", AGENDA_PATH);
    unlink(backup);
  }
  xSemaphoreGive(s_lock);
  /* A same-account token rotation keeps the binding generation.  Clearing
     the RTC alarm in that case would silently lose an otherwise valid
     reminder until a later agenda revision arrived. */
  if (binding_changed) rtc_clear_alarm();
}

esp_err_t agenda_schedule_next(void) {
  if (!s_lock) return ESP_ERR_INVALID_STATE;
  int64_t now = (int64_t)time(NULL);
  xSemaphoreTake(s_lock, portMAX_DELAY);
  int index = s_have_agenda ? luoye_agenda_next_index(&s_agenda, now) : -1;
  int64_t alarm = index >= 0 ? s_agenda.items[index].reminder_utc : 0;
  xSemaphoreGive(s_lock);
  return alarm > 0 ? rtc_set_alarm_utc(alarm) : rtc_clear_alarm();
}

bool agenda_take_due(int64_t now_utc, luoye_agenda_item_t *out) {
  if (!s_lock || !out) return false;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  int index = s_have_agenda ? luoye_agenda_due_index(&s_agenda, now_utc, 59) : -1;
  if (index >= 0 && s_agenda.items[index].reminder_utc < now_utc - 86400) index = -1;
  if (index >= 0) {
    *out = s_agenda.items[index];
    snprintf(s_active_reminder, sizeof(s_active_reminder), "%s", out->id);
  }
  xSemaphoreGive(s_lock);
  return index >= 0;
}

esp_err_t agenda_reminder_action(luoye_reminder_action_t action,
                                 int snooze_minutes) {
  if (!s_lock || !s_active_reminder[0]) return ESP_ERR_INVALID_STATE;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  luoye_agenda_item_t *active = NULL;
  for (uint8_t i = 0; i < s_agenda.count; i++) {
    if (strcmp(s_agenda.items[i].id, s_active_reminder) == 0) {
      active = &s_agenda.items[i];
      break;
    }
  }
  esp_err_t result = ESP_ERR_NOT_FOUND;
  if (active) {
    if (action == LUOYE_REMINDER_SNOOZE && snooze_minutes > 0) {
      active->reminder_utc = (int64_t)time(NULL) + snooze_minutes * 60LL;
    } else {
      active->dismissed = true;
    }
    result = agenda_save_locked();
  }
  s_active_reminder[0] = '\0';
  xSemaphoreGive(s_lock);
  if (result == ESP_OK) result = agenda_schedule_next();
  return result;
}

static const char *todo_state_name(luoye_todo_state_t state) {
  static const char *names[] = {
    "none", "capturing", "queued", "uploaded", "needs_confirmation",
    "confirm_pending", "cancel_pending", "created", "cancelled", "failed"
  };
  return state >= LUOYE_TODO_NONE && state <= LUOYE_TODO_FAILED
           ? names[state] : "failed";
}

static luoye_todo_state_t todo_state_parse(const char *state) {
  if (!state) return LUOYE_TODO_NONE;
  for (int i = LUOYE_TODO_NONE; i <= LUOYE_TODO_FAILED; i++) {
    if (strcmp(state, todo_state_name((luoye_todo_state_t)i)) == 0) {
      return (luoye_todo_state_t)i;
    }
  }
  return LUOYE_TODO_NONE;
}

static cJSON *todo_to_json(const luoye_todo_item_t *item) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return NULL;
  cJSON_AddStringToObject(root, "schema", "luoye-todo/1");
  cJSON_AddStringToObject(root, "todo_id", item->id);
  cJSON_AddStringToObject(root, "server_id", item->server_id);
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  cJSON_AddNumberToObject(root, "pcm_bytes", item->pcm_bytes);
  cJSON_AddNumberToObject(root, "result_revision", item->result_revision);
  cJSON_AddStringToObject(root, "state", todo_state_name(item->state));
  cJSON_AddNumberToObject(root, "last_http_status", item->last_http_status);
  cJSON_AddNumberToObject(root, "retry_count", item->retry_count);
  cJSON_AddNumberToObject(root, "created_utc", (double)item->created_utc);
  cJSON_AddNumberToObject(root, "due_utc", (double)item->due_utc);
  cJSON_AddStringToObject(root, "transcript", item->transcript);
  cJSON_AddStringToObject(root, "title", item->title);
  cJSON_AddStringToObject(root, "display_time", item->display_time);
  return root;
}

static bool todo_from_json(const char *directory, cJSON *root,
                           luoye_todo_item_t *out) {
  memset(out, 0, sizeof(*out));
  cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "todo_id");
  cJSON *server = cJSON_GetObjectItemCaseSensitive(root, "server_id");
  cJSON *state = cJSON_GetObjectItemCaseSensitive(root, "state");
  cJSON *transcript = cJSON_GetObjectItemCaseSensitive(root, "transcript");
  cJSON *title = cJSON_GetObjectItemCaseSensitive(root, "title");
  cJSON *display = cJSON_GetObjectItemCaseSensitive(root, "display_time");
  uint32_t binding = 0, pcm = 0, revision = 0, retry = 0;
  int64_t created = 0, due = 0;
  if (!cJSON_IsString(id) || !cJSON_IsString(server) || !cJSON_IsString(state) ||
      !cJSON_IsString(transcript) || !cJSON_IsString(title) ||
      !cJSON_IsString(display) ||
      !json_number_u32(root, "binding_generation", &binding) ||
      !json_number_u32(root, "pcm_bytes", &pcm) ||
      !json_number_u32(root, "result_revision", &revision) ||
      !json_number_u32(root, "retry_count", &retry) ||
      !json_number_i64(root, "created_utc", &created) ||
      !json_number_i64(root, "due_utc", &due) ||
      !todo_id_valid(id->valuestring) ||
      (server->valuestring[0] && !todo_id_valid(server->valuestring)) ||
      !luoye_agenda_text(out->transcript, sizeof(out->transcript), transcript->valuestring) ||
      !luoye_agenda_text(out->title, sizeof(out->title), title->valuestring) ||
      !luoye_agenda_text(out->display_time, sizeof(out->display_time),
                         display->valuestring)) return false;
  cJSON *http = cJSON_GetObjectItemCaseSensitive(root, "last_http_status");
  if (!cJSON_IsNumber(http) || http->valuedouble < 0 || http->valuedouble > 999) {
    return false;
  }
  snprintf(out->id, sizeof(out->id), "%s", id->valuestring);
  snprintf(out->server_id, sizeof(out->server_id), "%s", server->valuestring);
  const char *directory_id = strrchr(directory, '/');
  if (!directory_id || strcmp(directory_id + 1, out->id) != 0) return false;
  out->binding_generation = binding;
  out->pcm_bytes = pcm;
  out->result_revision = revision;
  out->retry_count = retry;
  out->last_http_status = (int)http->valuedouble;
  out->created_utc = created;
  out->due_utc = due;
  out->state = todo_state_parse(state->valuestring);
  snprintf(out->directory, sizeof(out->directory), "%s", directory);
  return binding > 0 && out->state != LUOYE_TODO_NONE;
}

esp_err_t todo_save(luoye_todo_item_t *item) {
  if (!s_lock || !item || !item->directory[0]) return ESP_ERR_INVALID_ARG;
  cJSON *root = todo_to_json(item);
  if (!root) return ESP_ERR_NO_MEM;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/todo.json", item->directory);
  xSemaphoreTake(s_lock, portMAX_DELAY);
  esp_err_t result = write_json_atomic(path, root);
  if (result == ESP_OK) s_latest_todo = *item;
  xSemaphoreGive(s_lock);
  cJSON_Delete(root);
  return result;
}

static bool todo_load_directory(const char *directory, luoye_todo_item_t *out) {
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/todo.json", directory);
  cJSON *root = read_json_one(path);
  if (root) {
    bool valid = todo_from_json(directory, root, out);
    cJSON_Delete(root);
    if (valid) return true;
  }
  char backup[PATH_BYTES];
  if (snprintf(backup, sizeof(backup), "%s.bak", path) >= (int)sizeof(backup)) {
    return false;
  }
  root = read_json_one(backup);
  if (!root) return false;
  bool valid = todo_from_json(directory, root, out);
  cJSON_Delete(root);
  return valid;
}

esp_err_t todo_generate_id(char *out, size_t out_size) {
  if (!out || out_size < 32) return ESP_ERR_INVALID_ARG;
  int length = snprintf(out, out_size, "TD-%lld-%08lx",
                        (long long)time(NULL), (unsigned long)esp_random());
  return length > 0 && (size_t)length < out_size ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

static void todo_audio_tap(const int16_t *mono, size_t frames,
                           void *user_ctx) {
  (void)user_ctx;
  if (!s_capture.active || !s_todo_ring || !mono) return;
  while (frames > 0) {
    size_t block = frames > 512 ? 512 : frames;
    xStreamBufferSend(s_todo_ring, mono, block * sizeof(int16_t), 0);
    mono += block;
    frames -= block;
  }
}

static bool todo_wav_commit(FILE *file, uint32_t pcm_bytes) {
  uint8_t header[LUOYE_WAV_HEADER_BYTES];
  luoye_wav_build_header(header, AUDIO_SAMPLE_RATE, 1, 16, pcm_bytes);
  if (fseek(file, 0, SEEK_SET) != 0 ||
      fwrite(header, 1, sizeof(header), file) != sizeof(header) ||
      fflush(file) != 0) return false;
  int fd = fileno(file);
  return fd >= 0 && fsync(fd) == 0;
}

static bool todo_recover_capture(luoye_todo_item_t *item) {
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
  FILE *file = fopen(path, "r+b");
  if (!file || fseek(file, 0, SEEK_END) != 0) {
    if (file) fclose(file);
    return false;
  }
  long length = ftell(file);
  if (length <= (long)LUOYE_WAV_HEADER_BYTES ||
      length > (long)(LUOYE_WAV_HEADER_BYTES + TODO_MAX_PCM)) {
    fclose(file);
    return false;
  }
  uint32_t pcm_bytes = (uint32_t)length - LUOYE_WAV_HEADER_BYTES;
  if (pcm_bytes & 1U) {
    pcm_bytes--;
    if (ftruncate(fileno(file), (off_t)(LUOYE_WAV_HEADER_BYTES + pcm_bytes)) != 0) {
      fclose(file);
      return false;
    }
  }
  bool ok = pcm_bytes > 0 && todo_wav_commit(file, pcm_bytes);
  if (fclose(file) != 0) ok = false;
  if (!ok) return false;
  item->pcm_bytes = pcm_bytes;
  item->state = LUOYE_TODO_QUEUED;
  item->retry_count = 0;
  item->last_http_status = 0;
  return todo_save(item) == ESP_OK;
}

static void todo_writer_task(void *argument) {
  (void)argument;
  uint8_t *buffer = heap_caps_malloc(TODO_WRITE_BYTES, MALLOC_CAP_INTERNAL);
  if (!buffer) vTaskDelete(NULL);
  for (;;) {
    if (!s_capture.active && !s_capture.closing) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    size_t got = xStreamBufferReceive(s_todo_ring, buffer, TODO_WRITE_BYTES,
                                      pdMS_TO_TICKS(50));
    if (got && s_capture.wav && !s_capture.write_error) {
      uint32_t room = s_capture.item.pcm_bytes < TODO_MAX_PCM
                        ? TODO_MAX_PCM - s_capture.item.pcm_bytes : 0;
      size_t write = got > room ? room : got;
      if (write && fwrite(buffer, 1, write, s_capture.wav) == write) {
        s_capture.item.pcm_bytes += (uint32_t)write;
        if (s_capture.item.pcm_bytes - s_capture.synced_pcm_bytes >=
            TODO_SYNC_BYTES) {
          int fd = fileno(s_capture.wav);
          if (fflush(s_capture.wav) != 0 || fd < 0 || fsync(fd) != 0) {
            s_capture.write_error = true;
          } else {
            s_capture.synced_pcm_bytes = s_capture.item.pcm_bytes;
          }
        }
      } else if (write) {
        s_capture.write_error = true;
      }
    }
    if (s_capture.closing && xStreamBufferBytesAvailable(s_todo_ring) == 0) {
      bool ok = s_capture.wav && !s_capture.write_error &&
                todo_wav_commit(s_capture.wav, s_capture.item.pcm_bytes);
      if (s_capture.wav && fclose(s_capture.wav) != 0) ok = false;
      s_capture.wav = NULL;
      s_capture.item.state = ok && s_capture.keep
                               ? LUOYE_TODO_QUEUED
                               : (s_capture.keep ? LUOYE_TODO_FAILED
                                                 : LUOYE_TODO_CANCELLED);
      if (todo_save(&s_capture.item) != ESP_OK) {
        s_capture.item.state = LUOYE_TODO_FAILED;
      }
      s_capture.active = false;
      s_capture.closing = false;
      xSemaphoreGive(s_todo_done);
    }
  }
}

esp_err_t todo_capture_begin(const char *todo_id,
                             uint32_t binding_generation) {
  if (!s_lock || !todo_id_valid(todo_id) || binding_generation == 0 ||
      !storage_sd_mounted()) return ESP_ERR_INVALID_ARG;
  if (s_capture.active || s_capture.closing) return ESP_ERR_INVALID_STATE;
  if (!make_dir(TODO_ROOT)) return ESP_FAIL;
  memset(&s_capture, 0, sizeof(s_capture));
  snprintf(s_capture.item.id, sizeof(s_capture.item.id), "%s", todo_id);
  if (!todo_directory(todo_id, s_capture.item.directory,
                      sizeof(s_capture.item.directory))) return ESP_ERR_INVALID_SIZE;
  if (!make_dir(s_capture.item.directory)) return ESP_FAIL;
  s_capture.item.binding_generation = binding_generation;
  s_capture.item.created_utc = (int64_t)time(NULL);
  s_capture.item.state = LUOYE_TODO_CAPTURING;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/audio.wav", s_capture.item.directory);
  s_capture.wav = fopen(path, "w+b");
  if (!s_capture.wav) return ESP_FAIL;
  uint8_t header[LUOYE_WAV_HEADER_BYTES] = {0};
  if (fwrite(header, 1, sizeof(header), s_capture.wav) != sizeof(header) ||
      fflush(s_capture.wav) != 0) {
    fclose(s_capture.wav);
    s_capture.wav = NULL;
    return ESP_FAIL;
  }
  xStreamBufferReset(s_todo_ring);
  s_capture.active = true;
  esp_err_t persist = todo_save(&s_capture.item);
  if (persist != ESP_OK) {
    s_capture.active = false;
    fclose(s_capture.wav);
    s_capture.wav = NULL;
    unlink(path);
    return persist;
  }
  audio_pdm_set_mono_tap(todo_audio_tap, NULL);
  return ESP_OK;
}

esp_err_t todo_capture_end(bool save, luoye_todo_item_t *out) {
  if (!s_capture.active || s_capture.closing) return ESP_ERR_INVALID_STATE;
  audio_pdm_set_mono_tap(NULL, NULL);
  s_capture.active = false;
  s_capture.keep = save;
  s_capture.closing = true;
  if (xSemaphoreTake(s_todo_done, pdMS_TO_TICKS(3000)) != pdTRUE) {
    return ESP_ERR_TIMEOUT;
  }
  if (out) *out = s_capture.item;
  return s_capture.item.state == LUOYE_TODO_QUEUED ? ESP_OK : ESP_FAIL;
}

bool todo_capture_active(void) {
  return s_capture.active || s_capture.closing;
}

bool todo_latest(luoye_todo_item_t *out) {
  if (!s_lock || !out) return false;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  bool available = s_latest_todo.state != LUOYE_TODO_NONE;
  if (available) *out = s_latest_todo;
  xSemaphoreGive(s_lock);
  return available;
}

esp_err_t todo_next(uint32_t binding_generation, luoye_todo_item_t *out) {
  if (!out || binding_generation == 0) return ESP_ERR_INVALID_ARG;
  DIR *root = opendir(TODO_ROOT);
  if (!root) return ESP_ERR_NOT_FOUND;
  esp_err_t result = ESP_ERR_NOT_FOUND;
  struct dirent *entry;
  while ((entry = readdir(root)) != NULL) {
    if (entry->d_name[0] == '.') continue;
    char directory[PATH_BYTES];
    if (!todo_directory(entry->d_name, directory, sizeof(directory))) continue;
    luoye_todo_item_t item;
    if (!todo_load_directory(directory, &item) ||
        item.binding_generation != binding_generation) continue;
    if (item.state == LUOYE_TODO_QUEUED ||
        item.state == LUOYE_TODO_UPLOADED ||
        item.state == LUOYE_TODO_CONFIRM_PENDING ||
        item.state == LUOYE_TODO_CANCEL_PENDING) {
      *out = item;
      result = ESP_OK;
      break;
    }
  }
  closedir(root);
  return result;
}

esp_err_t todo_read_audio(const luoye_todo_item_t *item,
                          uint8_t *buffer, size_t capacity, size_t *size_out) {
  if (!item || !buffer || !size_out) return ESP_ERR_INVALID_ARG;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
  FILE *file = fopen(path, "rb");
  if (!file) return ESP_ERR_NOT_FOUND;
  size_t length = 0;
  if (storage_sd_size(file, &length) != ESP_OK ||
      length < LUOYE_WAV_HEADER_BYTES || length > capacity ||
      storage_sd_seek(file, 0) != ESP_OK) {
    fclose(file);
    return ESP_ERR_INVALID_SIZE;
  }
  size_t got = 0;
  esp_err_t read_result = storage_sd_read(file, buffer, length, &got);
  fclose(file);
  if (read_result != ESP_OK || got != length) return ESP_FAIL;
  *size_out = got;
  return ESP_OK;
}

esp_err_t todo_set_result(const char *todo_id, const char *server_id,
                          uint32_t revision, const char *transcript,
                          const char *title, int64_t due_utc,
                          const char *display_time, bool needs_confirmation) {
  if (!todo_id_valid(todo_id) || !todo_id_valid(server_id) ||
      !transcript || !title || !display_time ||
      revision == 0 || due_utc < 0) return ESP_ERR_INVALID_ARG;
  char directory[PATH_BYTES];
  if (!todo_directory(todo_id, directory, sizeof(directory))) {
    return ESP_ERR_INVALID_ARG;
  }
  luoye_todo_item_t item;
  if (!todo_load_directory(directory, &item) || revision <= item.result_revision) {
    return ESP_ERR_INVALID_STATE;
  }
  snprintf(item.server_id, sizeof(item.server_id), "%s", server_id);
  if (!luoye_agenda_text(item.transcript, sizeof(item.transcript), transcript) ||
      !luoye_agenda_text(item.title, sizeof(item.title), title) ||
      !luoye_agenda_text(item.display_time, sizeof(item.display_time),
                         display_time)) return ESP_ERR_INVALID_RESPONSE;
  item.result_revision = revision;
  item.due_utc = due_utc;
  item.retry_count = 0;
  item.last_http_status = 0;
  item.state = needs_confirmation ? LUOYE_TODO_NEEDS_CONFIRMATION
                                  : LUOYE_TODO_CREATED;
  return todo_save(&item);
}

esp_err_t todo_request_action(uint32_t binding_generation, bool confirm) {
  luoye_todo_item_t item;
  if (!binding_generation || !todo_latest(&item) ||
      item.binding_generation != binding_generation ||
      item.state != LUOYE_TODO_NEEDS_CONFIRMATION) {
    return ESP_ERR_INVALID_STATE;
  }
  item.state = confirm ? LUOYE_TODO_CONFIRM_PENDING : LUOYE_TODO_CANCEL_PENDING;
  return todo_save(&item);
}

esp_err_t agenda_todo_init(void) {
  if (!storage_sd_mounted()) return ESP_ERR_INVALID_STATE;
  if (!make_dir(LUOYE_ROOT) || !make_dir(TODO_ROOT)) return ESP_FAIL;
  s_lock = xSemaphoreCreateMutex();
  s_todo_done = xSemaphoreCreateBinary();
  s_todo_ring = xStreamBufferCreateWithCaps(TODO_RING_BYTES, 1,
                                             MALLOC_CAP_SPIRAM);
  if (!s_lock || !s_todo_done || !s_todo_ring) return ESP_ERR_NO_MEM;
  cJSON *root = read_json_one(AGENDA_PATH);
  if (root) {
    luoye_agenda_snapshot_t loaded;
    if (parse_agenda(root, &loaded, true)) {
      s_agenda = loaded;
      s_have_agenda = true;
    }
    cJSON_Delete(root);
  }
  if (!s_have_agenda) {
    char backup[PATH_BYTES];
    snprintf(backup, sizeof(backup), "%s.bak", AGENDA_PATH);
    root = read_json_one(backup);
    if (root) {
      luoye_agenda_snapshot_t loaded;
      if (parse_agenda(root, &loaded, true)) {
        s_agenda = loaded;
        s_have_agenda = true;
      }
      cJSON_Delete(root);
    }
  }
  DIR *todos = opendir(TODO_ROOT);
  if (todos) {
    struct dirent *entry;
    while ((entry = readdir(todos)) != NULL) {
      if (entry->d_name[0] == '.') continue;
      char directory[PATH_BYTES];
      if (!todo_directory(entry->d_name, directory, sizeof(directory))) continue;
      luoye_todo_item_t item = {0};
      if (todo_load_directory(directory, &item) &&
          item.state == LUOYE_TODO_CAPTURING) {
        bool recovered = todo_recover_capture(&item);
        ESP_LOGW(TAG, "LY|TODO|id=%s phase=boot_recovery result=%s pcm=%lu",
                 item.id, recovered ? "queued" : "failed",
                 (unsigned long)item.pcm_bytes);
        if (!recovered) {
          item.state = LUOYE_TODO_FAILED;
          todo_save(&item);
        }
      }
      if (item.state != LUOYE_TODO_NONE &&
          item.created_utc >= s_latest_todo.created_utc) {
        s_latest_todo = item;
      }
    }
    closedir(todos);
  }
  if (xTaskCreatePinnedToCore(todo_writer_task, "todo_writer", 6144, NULL, 14,
                              NULL, 0) != pdPASS) return ESP_ERR_NO_MEM;
  if (s_have_agenda) agenda_schedule_next();
  ESP_LOGI(TAG, "LY|AGENDA|event=store_ready cached=%d revision=%lu items=%u",
           s_have_agenda, (unsigned long)s_agenda.revision, s_agenda.count);
  return ESP_OK;
}
