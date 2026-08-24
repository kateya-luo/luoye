#include "upload_store.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_vfs_fat.h"
#include "storage_sd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define SESSION_ROOT "/sdcard/rec"
#define PATH_BYTES 232
#define MAX_JSON_BYTES (64U * 1024U)

static SemaphoreHandle_t s_upload_lock;

static bool next_directory(DIR *root, char *directory, size_t directory_size,
                           char *session_id, size_t session_id_size);
static bool local_session_id_valid(const char *value);

static cJSON *read_json(const char *path) {
  FILE *file = fopen(path, "rb");
  if (!file) return NULL;
  size_t length = 0;
  if (storage_sd_size(file, &length) != ESP_OK || length > MAX_JSON_BYTES ||
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

static cJSON *read_json_with_backup(const char *path) {
  cJSON *root = read_json(path);
  if (root) return root;
  char backup[PATH_BYTES];
  if (snprintf(backup, sizeof(backup), "%s.bak", path) >= (int)sizeof(backup)) {
    return NULL;
  }
  return read_json(backup);
}

static esp_err_t sync_text(const char *path, const char *text) {
  FILE *file = fopen(path, "wb");
  if (!file) return ESP_FAIL;
  size_t size = strlen(text);
  esp_err_t result = fwrite(text, 1, size, file) == size ? ESP_OK : ESP_FAIL;
  if (result == ESP_OK && fflush(file) != 0) result = ESP_FAIL;
  int fd = fileno(file);
  if (result == ESP_OK && (fd < 0 || fsync(fd) != 0)) result = ESP_FAIL;
  if (fclose(file) != 0 && result == ESP_OK) result = ESP_FAIL;
  return result;
}

static esp_err_t write_json_atomic(const char *path, cJSON *root) {
  char *json = cJSON_PrintUnformatted(root);
  if (!json) return ESP_ERR_NO_MEM;
  cJSON *check = cJSON_Parse(json);
  if (!check) { cJSON_free(json); return ESP_ERR_INVALID_STATE; }
  cJSON_Delete(check);

  char temp[PATH_BYTES], backup[PATH_BYTES];
  if (snprintf(temp, sizeof(temp), "%s.tmp", path) >= (int)sizeof(temp) ||
      snprintf(backup, sizeof(backup), "%s.bak", path) >= (int)sizeof(backup)) {
    cJSON_free(json);
    return ESP_ERR_INVALID_SIZE;
  }
  esp_err_t result = sync_text(temp, json);
  cJSON_free(json);
  if (result != ESP_OK) { unlink(temp); return result; }
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

static void copy_json_string(cJSON *root, const char *name,
                             char *out, size_t out_size) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  const char *text = cJSON_IsString(value) ? value->valuestring : "";
  snprintf(out, out_size, "%s", text ? text : "");
}

static uint32_t json_u32(cJSON *root, const char *name) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  if (!cJSON_IsNumber(value) || value->valuedouble < 0 ||
      value->valuedouble > UINT32_MAX) return 0;
  return (uint32_t)value->valuedouble;
}

static int64_t json_optional_time(cJSON *root, const char *name) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  if (cJSON_IsNull(value) || !value) return 0;
  if (!cJSON_IsNumber(value) || value->valuedouble < 1577836800.0 ||
      value->valuedouble > 9007199254740991.0) return 0;
  int64_t number = (int64_t)value->valuedouble;
  return (double)number == value->valuedouble ? number : 0;
}

static bool json_bool(cJSON *root, const char *name) {
  return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, name));
}

static void set_string(cJSON *root, const char *name, const char *value) {
  cJSON_DeleteItemFromObjectCaseSensitive(root, name);
  cJSON_AddStringToObject(root, name, value ? value : "");
}

static void set_number(cJSON *root, const char *name, double value) {
  cJSON_DeleteItemFromObjectCaseSensitive(root, name);
  cJSON_AddNumberToObject(root, name, value);
}

static void set_bool(cJSON *root, const char *name, bool value) {
  cJSON_DeleteItemFromObjectCaseSensitive(root, name);
  cJSON_AddBoolToObject(root, name, value);
}

static cJSON *new_state(const char *session_id) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return NULL;
  cJSON_AddStringToObject(root, "schema", "luoye-upload/2");
  cJSON_AddStringToObject(root, "client_session_id", session_id);
  cJSON_AddStringToObject(root, "device_id", "");
  cJSON_AddNumberToObject(root, "binding_generation", 0);
  cJSON_AddStringToObject(root, "state", "queued");
  cJSON_AddStringToObject(root, "upload_mode", "live");
  cJSON_AddBoolToObject(root, "remote_session_created", false);
  cJSON_AddStringToObject(root, "server_session_id", "");
  cJSON_AddNumberToObject(root, "next_seq", 0);
  cJSON_AddNumberToObject(root, "live_chunk_bytes", 0);
  cJSON_AddNumberToObject(root, "acked_pcm_bytes", 0);
  cJSON_AddNumberToObject(root, "gap_start_bytes", 0);
  cJSON_AddBoolToObject(root, "live_resume_required", false);
  cJSON_AddBoolToObject(root, "deferred_gaps", false);
  cJSON_AddBoolToObject(root, "defer_acked", false);
  cJSON_AddBoolToObject(root, "marks_acked", false);
  cJSON_AddBoolToObject(root, "final_acked", false);
  cJSON_AddNumberToObject(root, "retry_count", 0);
  cJSON_AddNumberToObject(root, "last_http_status", 0);
  cJSON_AddNumberToObject(root, "result_revision", 0);
  cJSON_AddNumberToObject(root, "display_revision", 0);
  cJSON_AddNumberToObject(root, "caption_revision", 0);
  cJSON_AddNumberToObject(root, "speaker_revision", 0);
  cJSON_AddNumberToObject(root, "translation_revision", 0);
  cJSON_AddNumberToObject(root, "summary_revision", 0);
  cJSON_AddNumberToObject(root, "result_pcm_bytes", 0);
  return root;
}

static esp_err_t mirror_manifest(const sd_upload_item_t *item) {
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/session.json", item->directory);
  cJSON *root = read_json(path);
  if (!root) return ESP_ERR_INVALID_STATE;
  set_string(root, "device_id", item->device_id);
  set_number(root, "binding_generation", item->binding_generation);
  if (item->server_session_id[0]) {
    set_string(root, "server_session_id", item->server_session_id);
  }
  cJSON *upload = cJSON_GetObjectItemCaseSensitive(root, "upload");
  if (!cJSON_IsObject(upload)) {
    cJSON_DeleteItemFromObjectCaseSensitive(root, "upload");
    upload = cJSON_AddObjectToObject(root, "upload");
  }
  set_string(upload, "state", item->state);
  set_string(upload, "mode", item->upload_mode[0] ? item->upload_mode : "live");
  set_bool(upload, "remote_session_created", item->remote_session_created);
  set_number(upload, "next_seq", item->next_seq);
  set_number(upload, "live_chunk_bytes", item->live_chunk_bytes);
  set_number(upload, "acknowledged_bytes", item->acknowledged_bytes);
  set_number(upload, "gap_start_bytes", item->gap_start_bytes);
  set_bool(upload, "live_resume_required", item->live_resume_required);
  set_bool(upload, "deferred_gaps", item->deferred_gaps);
  set_bool(upload, "defer_acked", item->defer_acked);
  set_bool(upload, "marks_acked", item->marks_acked);
  set_bool(upload, "final_acked", item->final_acked);
  set_number(upload, "retry_count", item->retry_count);
  set_number(upload, "last_http_status", item->last_http_status);
  set_number(upload, "result_revision", item->result_revision);
  set_number(upload, "display_revision", item->display_revision);
  set_number(upload, "caption_revision", item->caption_revision);
  set_number(upload, "speaker_revision", item->speaker_revision);
  set_number(upload, "translation_revision", item->translation_revision);
  set_number(upload, "summary_revision", item->summary_revision);
  set_number(upload, "result_pcm_bytes", item->result_pcm_bytes);
  esp_err_t result = write_json_atomic(path, root);
  cJSON_Delete(root);
  return result;
}

static bool manifest_metadata(const char *directory, sd_upload_item_t *item,
                              bool allow_recording, bool *is_closed) {
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/session.json", directory);
  cJSON *manifest = read_json(path);
  if (!manifest) return false;
  cJSON *state = cJSON_GetObjectItemCaseSensitive(manifest, "state");
  const char *state_text = cJSON_IsString(state) ? state->valuestring : "";
  bool closed = strcmp(state_text, "local_closed") == 0 ||
                strcmp(state_text, "recovered") == 0;
  bool recording = strcmp(state_text, "local_recording") == 0 ||
                   strcmp(state_text, "recording") == 0;
  copy_json_string(manifest, "scene", item->scene, sizeof(item->scene));
  copy_json_string(manifest, "title", item->title, sizeof(item->title));
  item->started_at_utc = json_optional_time(manifest, "started_at_utc");
  item->ended_at_utc = json_optional_time(manifest, "ended_at_utc");
  cJSON_Delete(manifest);
  if (is_closed) *is_closed = closed;
  return closed || (allow_recording && recording);
}

static esp_err_t load_item(const char *directory, const char *session_id,
                           bool allow_recording, uint32_t safe_pcm_bytes,
                           sd_upload_item_t *item) {
  memset(item, 0, sizeof(*item));
  snprintf(item->directory, sizeof(item->directory), "%s", directory);
  snprintf(item->session_id, sizeof(item->session_id), "%s", session_id);
  bool closed = false;
  if (!manifest_metadata(directory, item, allow_recording, &closed)) {
    return ESP_ERR_INVALID_STATE;
  }
  item->local_closed = closed;

  char wav_path[PATH_BYTES], state_path[PATH_BYTES];
  snprintf(wav_path, sizeof(wav_path), "%s/audio.wav", directory);
  struct stat wav;
  if (stat(wav_path, &wav) != 0 || wav.st_size < 44) return ESP_ERR_NOT_FOUND;
  uint64_t actual_pcm = (uint64_t)wav.st_size - 44U;
  uint64_t pcm = closed ? actual_pcm : safe_pcm_bytes;
  if (!closed && pcm > actual_pcm) return ESP_ERR_INVALID_STATE;
  if (pcm > UINT32_MAX) return ESP_ERR_INVALID_SIZE;
  item->pcm_bytes = (uint32_t)pcm;

  snprintf(state_path, sizeof(state_path), "%s/upload.state", directory);
  cJSON *root = read_json_with_backup(state_path);
  if (!root) root = new_state(session_id);
  if (!root) return ESP_ERR_NO_MEM;
  copy_json_string(root, "server_session_id", item->server_session_id,
                   sizeof(item->server_session_id));
  copy_json_string(root, "device_id", item->device_id, sizeof(item->device_id));
  copy_json_string(root, "state", item->state, sizeof(item->state));
  copy_json_string(root, "upload_mode", item->upload_mode,
                   sizeof(item->upload_mode));
  if (!item->upload_mode[0]) {
    snprintf(item->upload_mode, sizeof(item->upload_mode), "live");
  }
  item->binding_generation = json_u32(root, "binding_generation");
  item->next_seq = json_u32(root, "next_seq");
  item->live_chunk_bytes = json_u32(root, "live_chunk_bytes");
  item->acknowledged_bytes = json_u32(root, "acked_pcm_bytes");
  item->gap_start_bytes = json_u32(root, "gap_start_bytes");
  item->retry_count = json_u32(root, "retry_count");
  item->last_http_status = (int)json_u32(root, "last_http_status");
  item->result_revision = json_u32(root, "result_revision");
  item->display_revision = json_u32(root, "display_revision");
  item->caption_revision = json_u32(root, "caption_revision");
  item->speaker_revision = json_u32(root, "speaker_revision");
  item->translation_revision = json_u32(root, "translation_revision");
  item->summary_revision = json_u32(root, "summary_revision");
  item->result_pcm_bytes = json_u32(root, "result_pcm_bytes");
  item->remote_session_created = json_bool(root, "remote_session_created");
  item->marks_acked = json_bool(root, "marks_acked");
  item->final_acked = json_bool(root, "final_acked");
  item->live_resume_required = json_bool(root, "live_resume_required");
  item->deferred_gaps = json_bool(root, "deferred_gaps");
  item->defer_acked = json_bool(root, "defer_acked");
  cJSON_Delete(root);
  if (item->acknowledged_bytes > item->pcm_bytes ||
      item->result_pcm_bytes > item->acknowledged_bytes) {
    return ESP_ERR_INVALID_STATE;
  }
  return ESP_OK;
}

esp_err_t sd_upload_store_init(void) {
  if (!s_upload_lock) s_upload_lock = xSemaphoreCreateMutex();
  return s_upload_lock ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t sd_upload_assign_identity(const char *session_id,
                                    const char *device_id,
                                    uint32_t binding_generation) {
  if (!s_upload_lock || !session_id || !*session_id || !device_id) {
    return ESP_ERR_INVALID_ARG;
  }
  char directory[SD_UPLOAD_DIR_BYTES], path[PATH_BYTES];
  snprintf(directory, sizeof(directory), SESSION_ROOT "/%s", session_id);
  snprintf(path, sizeof(path), "%s/upload.state", directory);
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  cJSON *root = read_json_with_backup(path);
  if (!root) root = new_state(session_id);
  esp_err_t result = root ? ESP_OK : ESP_ERR_NO_MEM;
  if (root) {
    set_string(root, "schema", "luoye-upload/2");
    set_string(root, "client_session_id", session_id);
    set_string(root, "device_id", device_id);
    set_number(root, "binding_generation", binding_generation);
    if (binding_generation == 0) set_string(root, "state", "auth_blocked");
    result = write_json_atomic(path, root);
    cJSON_Delete(root);
    if (result == ESP_OK) {
      sd_upload_item_t item = {0};
      snprintf(item.directory, sizeof(item.directory), "%s", directory);
      snprintf(item.session_id, sizeof(item.session_id), "%s", session_id);
      snprintf(item.device_id, sizeof(item.device_id), "%s", device_id);
      snprintf(item.state, sizeof(item.state), "%s",
               binding_generation ? "queued" : "auth_blocked");
      item.binding_generation = binding_generation;
      result = mirror_manifest(&item);
    }
  }
  xSemaphoreGive(s_upload_lock);
  return result;
}

static bool next_directory(DIR *root, char *directory, size_t directory_size,
                           char *session_id, size_t session_id_size) {
  struct dirent *entry;
  while ((entry = readdir(root)) != NULL) {
    if (entry->d_name[0] == '.' ||
        strlen(entry->d_name) >= session_id_size) continue;
    snprintf(directory, directory_size, SESSION_ROOT "/%s", entry->d_name);
    struct stat st;
    if (stat(directory, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
    snprintf(session_id, session_id_size, "%s", entry->d_name);
    return true;
  }
  return false;
}

esp_err_t sd_upload_next(uint32_t binding_generation, sd_upload_item_t *out) {
  if (!s_upload_lock || !out || binding_generation == 0) return ESP_ERR_INVALID_ARG;
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  DIR *root = opendir(SESSION_ROOT);
  if (!root) { xSemaphoreGive(s_upload_lock); return ESP_ERR_NOT_FOUND; }
  esp_err_t result = ESP_ERR_NOT_FOUND;
  sd_upload_item_t oldest = {0};
  char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
  while (next_directory(root, directory, sizeof(directory),
                        session_id, sizeof(session_id))) {
    sd_upload_item_t item;
    if (load_item(directory, session_id, false, 0, &item) != ESP_OK) continue;
    if (item.binding_generation != binding_generation) continue;
    if (!item.device_id[0] || strcmp(item.state, "permanent_error") == 0) continue;
    if (result != ESP_OK || item.started_at_utc < oldest.started_at_utc ||
        (item.started_at_utc == oldest.started_at_utc &&
         strcmp(item.session_id, oldest.session_id) < 0)) {
      oldest = item;
      result = ESP_OK;
    }
  }
  closedir(root);
  if (result == ESP_OK) *out = oldest;
  xSemaphoreGive(s_upload_lock);
  return result;
}

esp_err_t sd_upload_find(uint32_t binding_generation,
                         const char *session_id,
                         sd_upload_item_t *out) {
  if (!s_upload_lock || !binding_generation || !local_session_id_valid(session_id) ||
      !out) return ESP_ERR_INVALID_ARG;
  char directory[SD_UPLOAD_DIR_BYTES];
  if (snprintf(directory, sizeof(directory), SESSION_ROOT "/%s", session_id) >=
      (int)sizeof(directory)) return ESP_ERR_INVALID_SIZE;
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  sd_upload_item_t item;
  esp_err_t result = load_item(directory, session_id, false, 0, &item);
  if (result == ESP_OK &&
      (item.binding_generation != binding_generation || !item.device_id[0])) {
    result = ESP_ERR_NOT_FOUND;
  }
  if (result == ESP_OK) *out = item;
  xSemaphoreGive(s_upload_lock);
  return result;
}

esp_err_t sd_upload_current(uint32_t binding_generation, sd_upload_item_t *out) {
  if (!s_upload_lock || !out || binding_generation == 0) return ESP_ERR_INVALID_ARG;
  char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
  bool closed = false;
  uint32_t safe_pcm_bytes = 0;
  if (!sd_session_current(directory, sizeof(directory), session_id,
                          sizeof(session_id), &closed, &safe_pcm_bytes) || closed) {
    return ESP_ERR_NOT_FOUND;
  }
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  sd_upload_item_t item;
  esp_err_t result = load_item(directory, session_id, true, safe_pcm_bytes, &item);
  if (result == ESP_OK &&
      (item.binding_generation != binding_generation || !item.device_id[0] ||
       strcmp(item.state, "permanent_error") == 0)) {
    result = ESP_ERR_NOT_FOUND;
  }
  if (result == ESP_OK) *out = item;
  xSemaphoreGive(s_upload_lock);
  return result;
}

esp_err_t sd_upload_refresh_current(sd_upload_item_t *item) {
  if (!item || !item->session_id[0]) return ESP_ERR_INVALID_ARG;
  char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
  bool closed = false;
  uint32_t safe_pcm_bytes = 0;
  if (!sd_session_current(directory, sizeof(directory), session_id,
                          sizeof(session_id), &closed, &safe_pcm_bytes) ||
      strcmp(session_id, item->session_id) != 0 ||
      strcmp(directory, item->directory) != 0) {
    return ESP_ERR_NOT_FOUND;
  }
  if (safe_pcm_bytes < item->acknowledged_bytes) return ESP_ERR_INVALID_STATE;

  bool became_closed = closed && !item->local_closed;
  item->pcm_bytes = safe_pcm_bytes;
  item->local_closed = closed;
  if (became_closed) {
    /* session.json is committed before storage publishes closed=true.  Read
       only its immutable close metadata; never replace the newer RAM upload
       cursor with the deliberately older upload.state checkpoint. */
    xSemaphoreTake(s_upload_lock, portMAX_DELAY);
    sd_upload_item_t metadata = *item;
    bool manifest_closed = false;
    bool valid = manifest_metadata(directory, &metadata, true,
                                   &manifest_closed) && manifest_closed;
    xSemaphoreGive(s_upload_lock);
    if (!valid) return ESP_ERR_INVALID_STATE;
    item->ended_at_utc = metadata.ended_at_utc;
    snprintf(item->scene, sizeof(item->scene), "%s", metadata.scene);
    snprintf(item->title, sizeof(item->title), "%s", metadata.title);
  }
  return ESP_OK;
}

esp_err_t sd_upload_backlog(uint32_t *session_count, uint64_t *pending_bytes) {
  if (!s_upload_lock) return ESP_ERR_INVALID_STATE;
  uint32_t sessions = 0;
  uint64_t bytes = 0;
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  DIR *root = opendir(SESSION_ROOT);
  if (root) {
    char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
    while (next_directory(root, directory, sizeof(directory),
                          session_id, sizeof(session_id))) {
      sd_upload_item_t item;
      if (load_item(directory, session_id, false, 0, &item) != ESP_OK ||
          item.final_acked) continue;
      sessions++;
      bytes += item.pcm_bytes - item.acknowledged_bytes;
    }
    closedir(root);
  }
  xSemaphoreGive(s_upload_lock);
  if (session_count) *session_count = sessions;
  if (pending_bytes) *pending_bytes = bytes;
  return root ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t sd_upload_read_audio(const sd_upload_item_t *item,
                               uint32_t offset, void *buffer,
                               size_t wanted, size_t *received) {
  if (!item || !buffer || !received || offset > item->pcm_bytes ||
      wanted > item->pcm_bytes - offset) return ESP_ERR_INVALID_ARG;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
  FILE *file = fopen(path, "rb");
  if (!file) return ESP_ERR_NOT_FOUND;
  esp_err_t result = storage_sd_seek(file, 44U + offset);
  if (result == ESP_OK) result = storage_sd_read(file, buffer, wanted, received);
  else *received = 0;
  fclose(file);
  return result == ESP_OK && *received == wanted ? ESP_OK : ESP_FAIL;
}

void sd_upload_reader_close(sd_upload_reader_t *reader) {
  if (!reader) return;
  if (reader->file) fclose(reader->file);
  memset(reader, 0, sizeof(*reader));
}

esp_err_t sd_upload_reader_read(sd_upload_reader_t *reader,
                                const sd_upload_item_t *item,
                                uint32_t offset, void *buffer,
                                size_t wanted, size_t *received) {
  if (!reader || !item || !buffer || !received ||
      offset > item->pcm_bytes || wanted > item->pcm_bytes - offset) {
    return ESP_ERR_INVALID_ARG;
  }
  if (reader->file && strcmp(reader->session_id, item->session_id) != 0) {
    sd_upload_reader_close(reader);
  }
  if (!reader->file) {
    char path[PATH_BYTES];
    snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
    reader->file = fopen(path, "rb");
    if (!reader->file) return ESP_ERR_NOT_FOUND;
    snprintf(reader->session_id, sizeof(reader->session_id), "%s",
             item->session_id);
    reader->file_offset = 0;
  }

  uint32_t target = 44U + offset;
  if (reader->file_offset != target) {
    esp_err_t seek_error = storage_sd_seek(reader->file, target);
    if (seek_error != ESP_OK) {
      sd_upload_reader_close(reader);
      return seek_error;
    }
    reader->file_offset = target;
  }
  esp_err_t result = storage_sd_read(reader->file, buffer, wanted, received);
  if (result == ESP_OK && *received == wanted) {
    reader->file_offset += (uint32_t)*received;
    return ESP_OK;
  }
  sd_upload_reader_close(reader);
  return result == ESP_OK ? ESP_FAIL : result;
}

static bool sha256_hex_valid(const char *value) {
  if (!value || strlen(value) != SD_UPLOAD_SHA256_HEX_BYTES - 1) return false;
  for (size_t i = 0; i < SD_UPLOAD_SHA256_HEX_BYTES - 1; ++i) {
    if (!isxdigit((unsigned char)value[i])) return false;
  }
  return true;
}

esp_err_t sd_upload_range_sha256(const sd_upload_item_t *item,
                                 uint32_t offset, uint32_t length,
                                 char sha256[SD_UPLOAD_SHA256_HEX_BYTES]) {
  if (!item || !sha256 || !length || offset > item->pcm_bytes ||
      length > item->pcm_bytes - offset) return ESP_ERR_INVALID_ARG;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/%s", item->directory,
           SD_UPLOAD_RANGE_HASH_FILE);
  FILE *file = fopen(path, "rb");
  if (!file) return errno == ENOENT ? ESP_ERR_NOT_FOUND : ESP_FAIL;

  unsigned long row_offset = 0;
  unsigned long row_length = 0;
  char digest[SD_UPLOAD_SHA256_HEX_BYTES] = {0};
  esp_err_t result = ESP_ERR_NOT_FOUND;
  while (fscanf(file, "%lu %lu %64s", &row_offset, &row_length, digest) == 3) {
    if (row_offset == offset && row_length == length &&
        sha256_hex_valid(digest)) {
      for (size_t i = 0; i < SD_UPLOAD_SHA256_HEX_BYTES; ++i) {
        sha256[i] = (char)tolower((unsigned char)digest[i]);
      }
      result = ESP_OK;
      break;
    }
  }
  fclose(file);
  return result;
}

esp_err_t sd_upload_read_marks(const sd_upload_item_t *item,
                               void *buffer, size_t capacity,
                               size_t *received) {
  if (!item || !buffer || !capacity || !received) return ESP_ERR_INVALID_ARG;
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/marks.jsonl", item->directory);
  FILE *file = fopen(path, "rb");
  if (!file) return ESP_ERR_NOT_FOUND;
  size_t size = 0;
  if (storage_sd_size(file, &size) != ESP_OK || size > capacity ||
      storage_sd_seek(file, 0) != ESP_OK) {
    fclose(file);
    return ESP_ERR_INVALID_SIZE;
  }
  esp_err_t result = storage_sd_read(file, buffer, size, received);
  fclose(file);
  return result == ESP_OK && *received == size ? ESP_OK : ESP_FAIL;
}

esp_err_t sd_upload_save(sd_upload_item_t *item) {
  if (!s_upload_lock || !item || !item->session_id[0] ||
      item->acknowledged_bytes > item->pcm_bytes ||
      (item->remote_session_created && !item->server_session_id[0])) {
    return ESP_ERR_INVALID_ARG;
  }
  char path[PATH_BYTES];
  snprintf(path, sizeof(path), "%s/upload.state", item->directory);
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  cJSON *root = read_json_with_backup(path);
  if (!root) root = new_state(item->session_id);
  esp_err_t result = root ? ESP_OK : ESP_ERR_NO_MEM;
  if (root) {
    set_string(root, "schema", "luoye-upload/2");
    set_string(root, "client_session_id", item->session_id);
    set_string(root, "device_id", item->device_id);
    set_number(root, "binding_generation", item->binding_generation);
    set_string(root, "state", item->state);
    set_string(root, "upload_mode",
               item->upload_mode[0] ? item->upload_mode : "live");
    set_bool(root, "remote_session_created", item->remote_session_created);
    set_string(root, "server_session_id", item->server_session_id);
    set_number(root, "next_seq", item->next_seq);
    set_number(root, "live_chunk_bytes", item->live_chunk_bytes);
    set_number(root, "acked_pcm_bytes", item->acknowledged_bytes);
    set_number(root, "gap_start_bytes", item->gap_start_bytes);
    set_bool(root, "live_resume_required", item->live_resume_required);
    set_bool(root, "deferred_gaps", item->deferred_gaps);
    set_bool(root, "defer_acked", item->defer_acked);
    set_bool(root, "marks_acked", item->marks_acked);
    set_bool(root, "final_acked", item->final_acked);
    set_number(root, "retry_count", item->retry_count);
    set_number(root, "last_http_status", item->last_http_status);
    set_number(root, "result_revision", item->result_revision);
    set_number(root, "display_revision", item->display_revision);
    set_number(root, "caption_revision", item->caption_revision);
    set_number(root, "speaker_revision", item->speaker_revision);
    set_number(root, "translation_revision", item->translation_revision);
    set_number(root, "summary_revision", item->summary_revision);
    set_number(root, "result_pcm_bytes", item->result_pcm_bytes);
    result = write_json_atomic(path, root);
    cJSON_Delete(root);
    /* The writer owns session.json while recording; avoid cross-file rename races. */
    if (result == ESP_OK && item->local_closed) result = mirror_manifest(item);
  }
  xSemaphoreGive(s_upload_lock);
  return result;
}

static bool local_session_id_valid(const char *value) {
  if (!value || !value[0] || strlen(value) >= SD_UPLOAD_SESSION_ID_BYTES) {
    return false;
  }
  for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
    if (!((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
          (*p >= '0' && *p <= '9') || *p == '-' || *p == '_')) return false;
  }
  return true;
}

static uint64_t directory_bytes(const char *directory) {
  uint64_t bytes = 0;
  DIR *root = opendir(directory);
  if (!root) return 0;
  struct dirent *entry;
  char path[PATH_BYTES];
  while ((entry = readdir(root)) != NULL) {
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0 ||
        snprintf(path, sizeof(path), "%s/%s", directory, entry->d_name) >=
          (int)sizeof(path)) continue;
    struct stat st;
    if (stat(path, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) {
      bytes += (uint64_t)st.st_size;
    }
  }
  closedir(root);
  return bytes;
}

esp_err_t sd_storage_info(uint64_t *total_bytes, uint64_t *free_bytes) {
  uint64_t total = 0, free_space = 0;
  esp_err_t result = esp_vfs_fat_info("/sdcard", &total, &free_space);
  if (result == ESP_OK) {
    if (total_bytes) *total_bytes = total;
    if (free_bytes) *free_bytes = free_space;
  }
  return result;
}

static void inventory_insert(sd_storage_session_t *items, size_t capacity,
                             size_t *count, const sd_storage_session_t *candidate) {
  size_t position = 0;
  while (position < *count &&
         strcmp(items[position].session_id, candidate->session_id) < 0) position++;
  if (position >= capacity) return;
  size_t move_end = *count < capacity ? *count : capacity - 1U;
  while (move_end > position) {
    items[move_end] = items[move_end - 1U];
    move_end--;
  }
  items[position] = *candidate;
  if (*count < capacity) (*count)++;
}

esp_err_t sd_storage_inventory_page(uint32_t binding_generation,
                                    const char *after_session_id,
                                    sd_storage_session_t *items,
                                    size_t capacity, size_t *count,
                                    char *next_cursor, size_t next_cursor_size,
                                    bool *complete) {
  if (!s_upload_lock || !binding_generation || !items || !capacity ||
      capacity > SD_STORAGE_PAGE_MAX || !count || !complete ||
      !next_cursor || next_cursor_size == 0 ||
      (after_session_id && after_session_id[0] &&
       !local_session_id_valid(after_session_id))) return ESP_ERR_INVALID_ARG;
  *count = 0;
  *complete = true;
  next_cursor[0] = '\0';
  const char *after = after_session_id ? after_session_id : "";

  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  DIR *root = opendir(SESSION_ROOT);
  if (!root) {
    xSemaphoreGive(s_upload_lock);
    return errno == ENOENT ? ESP_OK : ESP_ERR_NOT_FOUND;
  }
  size_t eligible = 0;
  char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
  while (next_directory(root, directory, sizeof(directory),
                        session_id, sizeof(session_id))) {
    if (strcmp(session_id, after) <= 0) continue;
    sd_upload_item_t item;
    if (load_item(directory, session_id, false, 0, &item) != ESP_OK ||
        item.binding_generation != binding_generation) continue;
    eligible++;
    sd_storage_session_t candidate = {0};
    snprintf(candidate.session_id, sizeof(candidate.session_id), "%s", session_id);
    snprintf(candidate.server_session_id, sizeof(candidate.server_session_id),
             "%s", item.server_session_id);
    snprintf(candidate.state, sizeof(candidate.state), "%s", item.state);
    candidate.local_bytes = directory_bytes(directory);
    candidate.ended_at_utc = item.ended_at_utc;
    candidate.deletable = item.local_closed;
    inventory_insert(items, capacity, count, &candidate);
  }
  closedir(root);
  *complete = eligible <= capacity;
  if (*count) {
    snprintf(next_cursor, next_cursor_size, "%s", items[*count - 1U].session_id);
  }
  xSemaphoreGive(s_upload_lock);
  return ESP_OK;
}

static esp_err_t delete_session_locked(uint32_t binding_generation,
                                       const char *session_id,
                                       uint64_t *freed_bytes) {
  if (!local_session_id_valid(session_id)) return ESP_ERR_INVALID_ARG;
  char directory[SD_UPLOAD_DIR_BYTES];
  if (snprintf(directory, sizeof(directory), SESSION_ROOT "/%s", session_id) >=
      (int)sizeof(directory)) return ESP_ERR_INVALID_SIZE;
  /* Never leave a delete marker inside the directory that the recorder is
     still writing.  A cloud delete is valid at every upload stage, but an
     open FAT file must first be closed cleanly. */
  if (sd_session_is_open()) {
    char active[SD_UPLOAD_SESSION_ID_BYTES] = {0};
    bool closed = false;
    if (sd_session_current(NULL, 0, active, sizeof(active), &closed, NULL) &&
        !closed && strcmp(active, session_id) == 0) return ESP_ERR_INVALID_STATE;
  }
  char delete_marker[PATH_BYTES];
  snprintf(delete_marker, sizeof(delete_marker), "%s/delete.safe", directory);
  bool resume_delete = false;
  FILE *marker = fopen(delete_marker, "rb");
  if (marker) {
    unsigned long marked_generation = 0;
    resume_delete = fscanf(marker, "%lu", &marked_generation) == 1 &&
                    marked_generation == binding_generation;
    fclose(marker);
  }
  if (!resume_delete) {
    sd_upload_item_t item;
    if (load_item(directory, session_id, false, 0, &item) != ESP_OK) {
      return ESP_ERR_NOT_FOUND;
    }
    if (item.binding_generation != binding_generation) {
      return ESP_ERR_INVALID_STATE;
    }
    char generation[16];
    snprintf(generation, sizeof(generation), "%lu", (unsigned long)binding_generation);
    if (sync_text(delete_marker, generation) != ESP_OK) return ESP_FAIL;
  }
  DIR *root = opendir(directory);
  if (!root) return ESP_ERR_NOT_FOUND;
  uint64_t removed = 0;
  esp_err_t result = ESP_OK;
  char path[PATH_BYTES], manifest_path[PATH_BYTES] = {0};
  struct dirent *entry;
  /* Preflight the complete directory before unlinking anything.  Keep the
     authoritative session manifest until last, so a reset midway remains a
     retryable deletion instead of an apparently missing session. */
  while ((entry = readdir(root)) != NULL) {
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) continue;
    if (snprintf(path, sizeof(path), "%s/%s", directory, entry->d_name) >=
        (int)sizeof(path)) { result = ESP_ERR_INVALID_SIZE; break; }
    struct stat st;
    if (stat(path, &st) != 0 || !S_ISREG(st.st_mode)) {
      result = ESP_ERR_INVALID_STATE;
      break;
    }
    if (strcmp(entry->d_name, "session.json") == 0) {
      snprintf(manifest_path, sizeof(manifest_path), "%s", path);
    }
  }
  closedir(root);
  if (result != ESP_OK || (!manifest_path[0] && !resume_delete)) {
    return result != ESP_OK ? result : ESP_ERR_INVALID_STATE;
  }
  root = opendir(directory);
  if (!root) return ESP_ERR_NOT_FOUND;
  while ((entry = readdir(root)) != NULL) {
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0 ||
        strcmp(entry->d_name, "session.json") == 0 ||
        strcmp(entry->d_name, "delete.safe") == 0) continue;
    if (snprintf(path, sizeof(path), "%s/%s", directory, entry->d_name) >=
        (int)sizeof(path)) { result = ESP_ERR_INVALID_SIZE; break; }
    struct stat st;
    if (stat(path, &st) != 0 || unlink(path) != 0) { result = ESP_FAIL; break; }
    if (st.st_size > 0) removed += (uint64_t)st.st_size;
  }
  closedir(root);
  if (result == ESP_OK && manifest_path[0]) {
    struct stat manifest;
    if (stat(manifest_path, &manifest) != 0 || unlink(manifest_path) != 0) {
      result = ESP_FAIL;
    } else if (manifest.st_size > 0) {
      removed += (uint64_t)manifest.st_size;
    }
  }
  if (result == ESP_OK) {
    struct stat marker_stat;
    if (stat(delete_marker, &marker_stat) != 0 || unlink(delete_marker) != 0) {
      result = ESP_FAIL;
    } else if (marker_stat.st_size > 0) {
      removed += (uint64_t)marker_stat.st_size;
    }
  }
  if (result == ESP_OK && rmdir(directory) != 0) result = ESP_FAIL;
  if (result == ESP_OK && freed_bytes) *freed_bytes = removed;
  return result;
}

esp_err_t sd_storage_delete_local(uint32_t binding_generation,
                                  const char *session_id,
                                  uint64_t *freed_bytes) {
  if (!s_upload_lock || !binding_generation) return ESP_ERR_INVALID_ARG;
  if (freed_bytes) *freed_bytes = 0;
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  esp_err_t result = delete_session_locked(binding_generation, session_id,
                                           freed_bytes);
  xSemaphoreGive(s_upload_lock);
  return result;
}

esp_err_t sd_storage_delete_all_local(uint32_t binding_generation,
                                      uint32_t *deleted_count,
                                      uint64_t *freed_bytes) {
  if (!s_upload_lock || !binding_generation) return ESP_ERR_INVALID_ARG;
  uint32_t deleted = 0;
  uint64_t freed = 0;
  bool active_deferred = false;
  esp_err_t result = ESP_OK;
  xSemaphoreTake(s_upload_lock, portMAX_DELAY);
  for (;;) {
    char active_id[SD_UPLOAD_SESSION_ID_BYTES] = {0};
    bool active_closed = true;
    bool have_active = sd_session_current(NULL, 0, active_id,
                                          sizeof(active_id),
                                          &active_closed, NULL) &&
                       !active_closed;
    DIR *root = opendir(SESSION_ROOT);
    if (!root) break;
    char selected[SD_UPLOAD_SESSION_ID_BYTES] = {0};
    char directory[SD_UPLOAD_DIR_BYTES], session_id[SD_UPLOAD_SESSION_ID_BYTES];
    while (next_directory(root, directory, sizeof(directory),
                          session_id, sizeof(session_id))) {
      sd_upload_item_t item;
      if (load_item(directory, session_id, false, 0, &item) != ESP_OK ||
          item.binding_generation != binding_generation) continue;
      if (have_active && strcmp(active_id, session_id) == 0) {
        active_deferred = true;
        continue;
      }
      if (!selected[0] || strcmp(session_id, selected) < 0) {
        snprintf(selected, sizeof(selected), "%s", session_id);
      }
    }
    closedir(root);
    if (!selected[0]) break;
    uint64_t one = 0;
    result = delete_session_locked(binding_generation, selected, &one);
    if (result == ESP_ERR_INVALID_STATE) {
      active_deferred = true;
      result = ESP_OK;
      break;
    }
    if (result != ESP_OK && result != ESP_ERR_NOT_FOUND) break;
    if (result == ESP_OK) {
      deleted++;
      freed += one;
    }
    result = ESP_OK;
  }
  xSemaphoreGive(s_upload_lock);
  if (deleted_count) *deleted_count = deleted;
  if (freed_bytes) *freed_bytes = freed;
  if (result != ESP_OK) return result;
  return active_deferred ? ESP_ERR_INVALID_STATE : ESP_OK;
}
