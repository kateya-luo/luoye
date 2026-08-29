// net_uploader.c — Luoye API v1 provisioning, durable upload and cloud results.
//
// Security boundary:
//   * The local SoftAP page accepts WiFi credentials only.
//   * Account login/password never enter the recorder.
//   * The server binds a revocable device token to an authenticated account.
//   * Real audio upload stays gated until a device token has been issued.
#include "net_uploader.h"
#include "provisioning_form.h"
#include "upload_protocol.h"
#include "live_protocol.h"
#include "upload_store.h"
#include "storage_sd.h"
#include "agenda_todo.h"
#include "agenda_protocol.h"
#include "power_mgr.h"
#include "luoye_build_info.h"
#include "luoye_net_config.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

static const char *TAG = "net";

#define PUBLIC_SERVER_URL   LUOYE_CFG_SERVER_BASE_URL
#define LAN_WIFI_SSID       "TP-LINK_184F"
#define LAN_SERVER_URL      "http://192.168.31.183"
#define BUILD_INFO_PATH     "/api/v2/build-info"
#define PAIR_START_PATH     "/api/v2/device/pair/start"
#define PAIR_STATUS_PATH    "/api/v2/device/pair/status"
#define CHUNK_BYTES         (160U * 1024U)
#define RANGE_BLOCK_BYTES   SD_UPLOAD_RANGE_BLOCK_BYTES
#define RANGE_STREAM_BYTES  (16U * 1024U)
#define HTTP_TX_BUFFER_BYTES (16U * 1024U)
#define PORTAL_MAX_BODY     512
#define RESPONSE_BYTES      8192
#define MARKS_BUFFER_BYTES   (16 * 1024)
#define LIVE_POLL_MS         2000
#define LIVE_POLL_DEADLINE_MS 4000
#define AGENDA_POLL_MS      60000
#define TODO_AUDIO_BYTES    (1024 * 1024)
#define STORAGE_SYNC_MS     10000
#define STORAGE_SCAN_PAUSE_MS 60000
#define UPLOADER_STACK_BYTES (32 * 1024)
#define WIFI_PROFILE_MAX      8
#define WIFI_SCAN_MAX_APS     32

typedef struct {
  char ssid[33];
  char password[65];
} wifi_profile_t;

typedef struct {
  uint8_t *audio;
  uint8_t *range;
  uint8_t *marks;
  uint8_t *todo_audio;
} upload_task_buffers_t;

static net_post_fn s_post;
static volatile bool s_online;
static volatile bool s_bound;
static volatile bool s_cloud_ready;
static volatile bool s_manual_sync;
static volatile uint32_t s_manual_sync_request_revision;
static bool s_storage_fault_notified;
static volatile bool s_idle_suspended;
static volatile bool s_idle_resuming;
static volatile bool s_idle_agenda_maintenance;
static volatile bool s_idle_agenda_done;
static volatile bool s_idle_agenda_changed;
static volatile bool s_agenda_sync_requested;
static volatile bool s_live_gap_signal;
static volatile bool s_pairing_busy;
static volatile bool s_uploader_busy;
static bool s_bulk_wifi_ps_active;
static wifi_ps_type_t s_bulk_saved_wifi_ps = WIFI_PS_MIN_MODEM;
static bool s_wifi_started;
static bool s_wifi_connecting;
static bool s_have_credentials;
static bool s_pending_credentials;
static bool s_pair_requested;
static bool s_pair_registered;
static bool s_contract_checked;
static bool s_station_config_is_candidate;
static unsigned s_pending_attempts;
static uint32_t s_binding_generation;
static uint32_t s_pair_poll_ms = 5000;

static esp_netif_t *s_sta_netif;
static esp_netif_t *s_ap_netif;
static httpd_handle_t s_portal;
static esp_timer_handle_t s_reconnect_timer;
static SemaphoreHandle_t s_pair_lock;
static SemaphoreHandle_t s_live_lock;
static luoye_live_result_t s_live;
static TaskHandle_t s_time_task;
static TaskHandle_t s_wifi_selector_task;
static volatile bool s_use_lan_server;
static volatile int64_t s_offline_since_ms;
static int64_t s_next_live_poll_ms;

static char s_token[192];
static char s_saved_ssid[33];
static char s_saved_pass[65];
static wifi_profile_t s_wifi_profiles[WIFI_PROFILE_MAX];
static uint8_t s_wifi_profile_count;
static int s_active_wifi_profile = -1;
static int s_failed_wifi_profile = -1;
static char s_pending_ssid[33];
static char s_pending_pass[65];
static char s_pair_nonce[33];
static char s_live_session_id[SD_UPLOAD_SESSION_ID_BYTES];
static char s_bulk_delete_command[73];
static uint32_t s_bulk_deleted_count;
static uint64_t s_bulk_freed_bytes;
static upload_task_buffers_t s_upload_buffers;
static net_pairing_info_t s_pair;
static void cloud_set_ready(bool ready);
static void wifi_selector_notify(void);

static void upload_buffers_release(void) {
  heap_caps_free(s_upload_buffers.audio);
  heap_caps_free(s_upload_buffers.range);
  heap_caps_free(s_upload_buffers.marks);
  heap_caps_free(s_upload_buffers.todo_audio);
  memset(&s_upload_buffers, 0, sizeof(s_upload_buffers));
}

static void wifi_retry_schedule(void) {
  if (!s_reconnect_timer) return;
  esp_timer_stop(s_reconnect_timer);
  esp_err_t error = esp_timer_start_once(s_reconnect_timer, 5 * 1000 * 1000);
  if (error != ESP_OK) {
    ESP_LOGW(TAG, "LY|IDLE_NET|state=retry_schedule result=%s",
             esp_err_to_name(error));
  }
}

static const char *server_base_url(void) {
  return s_use_lan_server ? LAN_SERVER_URL : PUBLIC_SERVER_URL;
}

static const char PORTAL_HTML[] =
  "<!doctype html><html lang=\"zh-CN\"><head>"
  "<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
  "<title>落叶配网</title><style>"
  "body{font:16px system-ui;margin:0;background:#f4f1ea;color:#171716}"
  "main{max-width:420px;margin:32px auto;padding:24px}"
  "section{background:#fffdf8;border:1px solid #d9d4c8;border-radius:18px;padding:22px}"
  "h1{font-size:25px;margin:0 0 8px}p{line-height:1.55;color:#5e5d55}"
  "label{display:block;margin-top:16px;font-weight:700}"
  "input{width:100%;box-sizing:border-box;margin-top:7px;padding:12px;border:1px solid #aaa;border-radius:10px;font-size:16px}"
  "button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:10px;background:#171716;color:white;font-size:16px}"
  "small{display:block;margin-top:16px;line-height:1.5;color:#6b6b63}</style></head><body><main><section>"
  "<h1>落叶配网</h1><p><strong>ENGINEERING 联调固件</strong></p>"
  "<p>选择 2.4 GHz WiFi。设备只保存 WiFi，不接收 ClearMeeting 账号密码。</p>"
  "<form id=\"provisioning\" method=\"post\" action=\"/api/provision\">"
  "<label>WiFi 名称<input name=\"ssid\" list=\"networks\" maxlength=\"32\" required autocomplete=\"off\"></label>"
  "<datalist id=\"networks\"></datalist>"
  "<label>目标 WiFi 密码（开放网络可留空）<input name=\"password\" type=\"password\" maxlength=\"63\" autocomplete=\"new-password\"></label>"
  "<input type=\"hidden\" name=\"client_time_utc\" id=\"client_time_utc\">"
  "<button type=\"submit\">连接 WiFi</button></form>"
  "<small>连接成功后，请按墨水屏提示登录 ClearMeeting 完成账号认领。账号密码不会发送给录音卡。</small>"
  "</section></main><script>"
  "document.querySelector('#provisioning').addEventListener('submit',()=>{"
  "document.querySelector('#client_time_utc').value=String(Math.floor(Date.now()/1000))});"
  "fetch('/api/networks').then(r=>r.json()).then(x=>{let d=document.querySelector('#networks');"
  "(x.networks||[]).forEach(n=>{let o=document.createElement('option');o.value=n.ssid;d.appendChild(o)})}).catch(()=>{});"
  "</script></body></html>";

static void pair_copy_locked(net_pairing_info_t *out) {
  if (s_pair_lock) xSemaphoreTake(s_pair_lock, portMAX_DELAY);
  *out = s_pair;
  if (s_pair_lock) xSemaphoreGive(s_pair_lock);
}

void net_get_pairing_info(net_pairing_info_t *out) {
  if (!out) return;
  pair_copy_locked(out);
}

static void pair_set_state(net_pair_state_t state, esp_err_t error, int http_status) {
  bool changed;
  if (s_pair_lock) xSemaphoreTake(s_pair_lock, portMAX_DELAY);
  changed = s_pair.state != state ||
            s_pair.last_error != error ||
            s_pair.last_http_status != http_status;
  s_pair.state = state;
  s_pair.last_error = error;
  s_pair.last_http_status = http_status;
  if (s_pair_lock) xSemaphoreGive(s_pair_lock);
  if (!changed) return;
  if (s_post) s_post(APP_EV_PAIRING_CHANGE, (int32_t)state);
  ESP_LOGI(TAG, "LY|PAIR|state=%d online=%d bound=%d err=%s http=%d",
           (int)state, s_online, s_bound, esp_err_to_name(error), http_status);
}

static void pair_set_text(const char *code, const char *account) {
  if (s_pair_lock) xSemaphoreTake(s_pair_lock, portMAX_DELAY);
  if (code) {
    strlcpy(s_pair.pairing_code, code, sizeof(s_pair.pairing_code));
  }
  if (account) {
    strlcpy(s_pair.masked_account, account, sizeof(s_pair.masked_account));
  }
  if (s_pair_lock) xSemaphoreGive(s_pair_lock);
}

bool net_is_online(void) { return s_online; }
bool net_is_bound(void) { return s_bound; }
bool net_is_cloud_ready(void) { return s_cloud_ready; }

static void bulk_wifi_ps_update(bool bulk_upload_active) {
  if (bulk_upload_active) {
    if (s_bulk_wifi_ps_active) return;
    wifi_ps_type_t previous = WIFI_PS_MIN_MODEM;
    esp_err_t error = esp_wifi_get_ps(&previous);
    if (error == ESP_OK) error = esp_wifi_set_ps(WIFI_PS_NONE);
    if (error == ESP_OK) {
      s_bulk_saved_wifi_ps = previous;
      s_bulk_wifi_ps_active = true;
      ESP_LOGI(TAG,
               "LY|UPLOAD_WIFI_PS|state=performance previous=%d active=%d result=ESP_OK",
               (int)previous, (int)WIFI_PS_NONE);
    } else {
      ESP_LOGW(TAG,
               "LY|UPLOAD_WIFI_PS|state=performance result=%s keep_previous=1",
               esp_err_to_name(error));
    }
    return;
  }
  if (!s_bulk_wifi_ps_active) return;
  esp_err_t error = esp_wifi_set_ps(s_bulk_saved_wifi_ps);
  if (error == ESP_OK) {
    ESP_LOGI(TAG,
             "LY|UPLOAD_WIFI_PS|state=restored mode=%d result=ESP_OK",
             (int)s_bulk_saved_wifi_ps);
    s_bulk_wifi_ps_active = false;
  } else {
    ESP_LOGW(TAG,
             "LY|UPLOAD_WIFI_PS|state=restore_failed mode=%d result=%s retry=1",
             (int)s_bulk_saved_wifi_ps, esp_err_to_name(error));
  }
}

static void stop_for_storage_fault(const char *phase, esp_err_t error) {
  bool manual_was_running = s_manual_sync;
  s_manual_sync = false;
  bulk_wifi_ps_update(false);
  bool first_notification = !s_storage_fault_notified;
  if (first_notification) {
    s_storage_fault_notified = true;
    ESP_LOGE(TAG,
             "LY|SYNC|state=failed reason=storage_fault phase=%s esp=%s local_ack=unchanged",
             phase ? phase : "unknown", esp_err_to_name(error));
  }
  /* Every explicit retry must leave the UI in FAILED, even though the root
     storage-fault log and recording error notification are emitted once. */
  if (manual_was_running && s_post) {
    s_post(APP_EV_SYNC_CHANGE, APP_SYNC_FAILED);
  }
  if (first_notification && sd_session_is_open() && s_post) {
    s_post(APP_EV_STORAGE_ERROR, APP_ERR_STORAGE_SYNC);
  }
}

static void stop_manual_sync_scan_error(const char *phase, esp_err_t error) {
  if (!s_manual_sync) return;
  s_manual_sync = false;
  bulk_wifi_ps_update(false);
  if (s_post) s_post(APP_EV_SYNC_CHANGE, APP_SYNC_FAILED);
  ESP_LOGE(TAG,
           "LY|SYNC|state=failed reason=local_scan phase=%s esp=%s local_ack=unchanged",
           phase ? phase : "unknown", esp_err_to_name(error));
}

bool net_can_idle(void) {
  if (s_pair_requested || s_pending_credentials || s_idle_resuming ||
      s_pairing_busy || s_uploader_busy || s_manual_sync) return false;
  if (s_live_session_id[0] == '\0') return true;
  if (s_online || s_offline_since_ms <= 0) return false;
  int64_t now_ms = esp_timer_get_time() / 1000;
  return now_ms - s_offline_since_ms >= 60000;
}

static esp_err_t idle_stop_wifi(void) {
  if (s_reconnect_timer) esp_timer_stop(s_reconnect_timer);
  esp_err_t error = ESP_OK;
  if (s_wifi_started) {
    error = esp_wifi_stop();
    if (error == ESP_OK || error == ESP_ERR_WIFI_NOT_STARTED) {
      s_wifi_started = false;
      s_wifi_connecting = false;
      error = ESP_OK;
    }
  }
  s_contract_checked = false;
  cloud_set_ready(false);
  if (s_online) {
    s_online = false;
    if (s_post) s_post(APP_EV_NET_CHANGE, 0);
  }
  return error;
}

esp_err_t net_idle_suspend(void) {
  if (!net_can_idle()) return ESP_ERR_INVALID_STATE;
  if (s_idle_suspended && !s_idle_resuming) return ESP_OK;
  /* Close the scheduler race before stopping Wi-Fi.  A request that passed
     its outer gate just before this flag was raised will publish its busy
     flag; requests that have not passed the gate are now held back. */
  s_idle_suspended = true;
  s_idle_resuming = false;
  for (int i = 0; i < 20 && (s_pairing_busy || s_uploader_busy); ++i) {
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  if (s_pairing_busy || s_uploader_busy) {
    s_idle_suspended = false;
    ESP_LOGD(TAG, "LY|IDLE_NET|state=suspend_deferred reason=request_busy");
    return ESP_ERR_INVALID_STATE;
  }
  esp_err_t error = idle_stop_wifi();
  if (error != ESP_OK) {
    s_idle_suspended = false;
    wifi_retry_schedule();
    ESP_LOGW(TAG, "LY|IDLE_NET|state=suspend_failed result=%s",
             esp_err_to_name(error));
    return error;
  }
  ESP_LOGI(TAG, "LY|IDLE_NET|state=suspended result=%s",
           esp_err_to_name(error));
  return error;
}

esp_err_t net_idle_resume(void) {
  s_idle_agenda_maintenance = false;
  s_idle_agenda_done = false;
  if (!s_idle_suspended && !s_idle_resuming) return ESP_OK;
  if (!s_have_credentials) {
    s_idle_suspended = false;
    s_idle_resuming = false;
    ESP_LOGI(TAG,
             "LY|IDLE_NET|state=resumed result=ESP_OK reason=no_credentials");
    return ESP_OK;
  }
  if (s_online) {
    s_idle_suspended = false;
    s_idle_resuming = false;
    ESP_LOGI(TAG, "LY|IDLE_NET|state=resumed result=ESP_OK reason=already_ip");
    return ESP_OK;
  }
  s_idle_resuming = true;
  /* Keep s_idle_suspended asserted until IP_EVENT_STA_GOT_IP.  The selector
     task owns start/scan/connect so failures can be retried without blocking
     key handling or local recording. */
  wifi_selector_notify();
  ESP_LOGI(TAG,
           "LY|IDLE_NET|state=resume_requested result=ESP_OK retry_ms=5000");
  return ESP_OK;
}

bool net_idle_is_suspended(void) {
  return s_idle_suspended || s_idle_resuming || s_idle_agenda_maintenance;
}

bool net_idle_agenda_maintenance_start(void) {
  if (!s_idle_suspended || s_idle_resuming || s_idle_agenda_maintenance ||
      !s_have_credentials || !s_bound || s_pair_requested ||
      s_pairing_busy || s_uploader_busy || s_manual_sync ||
      s_live_session_id[0]) {
    return false;
  }
  s_idle_agenda_changed = false;
  s_idle_agenda_done = false;
  s_idle_agenda_maintenance = true;
  s_idle_resuming = true;
  wifi_selector_notify();
  ESP_LOGI(TAG, "LY|IDLE_AGENDA|state=start wifi=connecting lane=agenda_only");
  return true;
}

bool net_idle_agenda_maintenance_done(bool *changed) {
  if (changed) *changed = s_idle_agenda_changed;
  return s_idle_agenda_done;
}

esp_err_t net_idle_agenda_maintenance_stop(void) {
  if (!s_idle_agenda_maintenance && !s_idle_resuming) return ESP_OK;
  if (s_uploader_busy || s_pairing_busy) return ESP_ERR_INVALID_STATE;
  s_idle_agenda_maintenance = false;
  s_idle_agenda_done = false;
  s_idle_resuming = false;
  s_idle_suspended = true;
  esp_err_t error = idle_stop_wifi();
  ESP_LOGI(TAG, "LY|IDLE_AGENDA|state=stop wifi=off result=%s changed=%d",
           esp_err_to_name(error), s_idle_agenda_changed);
  return error;
}

bool net_request_agenda_sync(void) {
  if (!s_bound || s_binding_generation == 0) return false;
  s_agenda_sync_requested = true;
  ESP_LOGI(TAG, "LY|AGENDA|event=force_requested online=%d cloud=%d",
           s_online, s_cloud_ready);
  return true;
}

bool net_request_manual_sync(void) {
  if (!s_bound || s_binding_generation == 0 || !storage_sd_mounted()) {
    return false;
  }
  s_manual_sync = true;
  uint32_t revision = s_manual_sync_request_revision + 1U;
  s_manual_sync_request_revision = revision ? revision : 1U;
  if (s_post) s_post(APP_EV_SYNC_CHANGE, APP_SYNC_RUNNING);
  ESP_LOGI(TAG,
           "LY|SYNC|state=requested revision=%lu action=rearm_upload_plan online=%d cloud=%d",
           (unsigned long)s_manual_sync_request_revision, s_online,
           s_cloud_ready);
  return true;
}
uint32_t net_binding_generation(void) {
  return s_bound ? s_binding_generation : 0;
}

bool net_live_snapshot(luoye_live_result_t *out) {
  if (!out || !s_live_lock) return false;
  xSemaphoreTake(s_live_lock, portMAX_DELAY);
  *out = s_live;
  bool available = s_live.kind != LUOYE_LIVE_NONE;
  xSemaphoreGive(s_live_lock);
  return available;
}

static void live_reset(const char *client_session_id) {
  if (!s_live_lock) return;
  xSemaphoreTake(s_live_lock, portMAX_DELAY);
  memset(&s_live, 0, sizeof(s_live));
  if (client_session_id) {
    luoye_live_set_text(s_live.client_session_id,
                        sizeof(s_live.client_session_id), client_session_id);
  }
  xSemaphoreGive(s_live_lock);
}

static void cloud_set_ready(bool ready) {
  if (s_cloud_ready == ready) return;
  s_cloud_ready = ready;
  if (s_post) s_post(APP_EV_CLOUD_CHANGE, ready ? 1 : 0);
  ESP_LOGI(TAG, "LY|CLOUD|ready=%d wifi=%d", ready, s_online);
}

static void random_digits(char *out, size_t digits) {
  for (size_t i = 0; i < digits; i++) out[i] = (char)('0' + esp_random() % 10);
  out[digits] = '\0';
}

static void random_hex(char *out, size_t bytes) {
  static const char HEX[] = "0123456789abcdef";
  for (size_t i = 0; i < bytes; i++) {
    uint8_t value = (uint8_t)esp_random();
    out[i * 2] = HEX[value >> 4];
    out[i * 2 + 1] = HEX[value & 0x0f];
  }
  out[bytes * 2] = '\0';
}

static bool nvs_get_string(nvs_handle_t nvs, const char *key, char *out, size_t size) {
  size_t length = size;
  if (nvs_get_str(nvs, key, out, &length) != ESP_OK) {
    out[0] = '\0';
    return false;
  }
  out[size - 1] = '\0';
  return out[0] != '\0';
}

static void load_nvs_config(void) {
  nvs_handle_t nvs;
  if (nvs_open("net", NVS_READONLY, &nvs) != ESP_OK) return;
  uint8_t count = 0;
  if (nvs_get_u8(nvs, "wcnt", &count) != ESP_OK || count > WIFI_PROFILE_MAX) count = 0;
  for (uint8_t i = 0; i < count; i++) {
    char ssid_key[8], pass_key[8];
    snprintf(ssid_key, sizeof(ssid_key), "ws%u", (unsigned)i);
    snprintf(pass_key, sizeof(pass_key), "wp%u", (unsigned)i);
    if (!nvs_get_string(nvs, ssid_key, s_wifi_profiles[s_wifi_profile_count].ssid,
                        sizeof(s_wifi_profiles[0].ssid))) continue;
    nvs_get_string(nvs, pass_key, s_wifi_profiles[s_wifi_profile_count].password,
                   sizeof(s_wifi_profiles[0].password));
    s_wifi_profile_count++;
  }
  /* Seamless migration from the former single-network schema. */
  if (s_wifi_profile_count == 0 &&
      nvs_get_string(nvs, "ssid", s_wifi_profiles[0].ssid,
                     sizeof(s_wifi_profiles[0].ssid))) {
    nvs_get_string(nvs, "pass", s_wifi_profiles[0].password,
                   sizeof(s_wifi_profiles[0].password));
    s_wifi_profile_count = 1;
  }
  if (s_wifi_profile_count > 0) {
    strlcpy(s_saved_ssid, s_wifi_profiles[0].ssid, sizeof(s_saved_ssid));
    strlcpy(s_saved_pass, s_wifi_profiles[0].password, sizeof(s_saved_pass));
  }
  nvs_get_string(nvs, "token", s_token, sizeof(s_token));
  nvs_get_string(nvs, "account", s_pair.masked_account, sizeof(s_pair.masked_account));
  if (nvs_get_u32(nvs, "binding_gen", &s_binding_generation) != ESP_OK) {
    s_binding_generation = 0;
  }
  nvs_close(nvs);
  s_have_credentials = s_wifi_profile_count > 0;
  s_bound = s_token[0] != '\0' && s_binding_generation > 0;
  if (!s_bound) memset(s_token, 0, sizeof(s_token));
}

static esp_err_t save_credentials(const char *ssid, const char *password) {
  wifi_profile_t updated[WIFI_PROFILE_MAX] = {0};
  strlcpy(updated[0].ssid, ssid, sizeof(updated[0].ssid));
  strlcpy(updated[0].password, password, sizeof(updated[0].password));
  uint8_t updated_count = 1;
  for (uint8_t i = 0; i < s_wifi_profile_count && updated_count < WIFI_PROFILE_MAX; i++) {
    if (strcmp(s_wifi_profiles[i].ssid, ssid) == 0) continue;
    updated[updated_count++] = s_wifi_profiles[i];
  }
  nvs_handle_t nvs;
  esp_err_t error = nvs_open("net", NVS_READWRITE, &nvs);
  if (error != ESP_OK) return error;
  error = nvs_set_u8(nvs, "wcnt", updated_count);
  for (uint8_t i = 0; error == ESP_OK && i < WIFI_PROFILE_MAX; i++) {
    char ssid_key[8], pass_key[8];
    snprintf(ssid_key, sizeof(ssid_key), "ws%u", (unsigned)i);
    snprintf(pass_key, sizeof(pass_key), "wp%u", (unsigned)i);
    if (i < updated_count) {
      error = nvs_set_str(nvs, ssid_key, updated[i].ssid);
      if (error == ESP_OK) error = nvs_set_str(nvs, pass_key, updated[i].password);
    } else {
      esp_err_t erased = nvs_erase_key(nvs, ssid_key);
      if (erased != ESP_OK && erased != ESP_ERR_NVS_NOT_FOUND) error = erased;
      erased = nvs_erase_key(nvs, pass_key);
      if (error == ESP_OK && erased != ESP_OK && erased != ESP_ERR_NVS_NOT_FOUND) error = erased;
    }
  }
  /* Keep the legacy keys for downgrade compatibility. */
  if (error == ESP_OK) error = nvs_set_str(nvs, "ssid", ssid);
  if (error == ESP_OK) error = nvs_set_str(nvs, "pass", password);
  if (error == ESP_OK) error = nvs_commit(nvs);
  nvs_close(nvs);
  if (error == ESP_OK) {
    memcpy(s_wifi_profiles, updated, sizeof(updated));
    s_wifi_profile_count = updated_count;
  }
  return error;
}

static void wifi_selector_notify(void) {
  if (s_wifi_selector_task) xTaskNotifyGive(s_wifi_selector_task);
}

static int wifi_profile_for_ssid(const uint8_t *ssid) {
  for (uint8_t i = 0; i < s_wifi_profile_count; i++) {
    if (strncmp(s_wifi_profiles[i].ssid, (const char *)ssid,
                sizeof(s_wifi_profiles[i].ssid)) == 0) return i;
  }
  return -1;
}

static void wifi_selector_task(void *argument) {
  (void)argument;
  while (true) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if ((s_idle_suspended && !s_idle_resuming) || s_online || s_wifi_connecting ||
        s_pending_credentials || s_pair_requested || !s_have_credentials) {
      continue;
    }

    if (!s_wifi_started) {
      esp_err_t start_error = esp_wifi_set_mode(WIFI_MODE_STA);
      if (start_error == ESP_OK) start_error = esp_wifi_start();
      if (start_error != ESP_OK) {
        ESP_LOGW(TAG,
                 "LY|IDLE_NET|state=retry phase=wifi_start result=%s",
                 esp_err_to_name(start_error));
        wifi_retry_schedule();
        continue;
      }
      s_wifi_started = true;
      ESP_LOGI(TAG, "LY|IDLE_NET|state=radio_started result=ESP_OK");
    }

    wifi_scan_config_t scan = {0};
    esp_err_t scan_error = esp_wifi_scan_start(&scan, true);
    uint16_t count = 0;
    wifi_ap_record_t *records = NULL;
    if (scan_error == ESP_OK) {
      esp_wifi_scan_get_ap_num(&count);
      if (count > WIFI_SCAN_MAX_APS) count = WIFI_SCAN_MAX_APS;
      if (count > 0) records = calloc(count, sizeof(*records));
      if (count > 0 && (!records || esp_wifi_scan_get_ap_records(&count, records) != ESP_OK)) {
        free(records);
        records = NULL;
        count = 0;
      }
    }

    int selected = -1;
    int selected_rssi = -128;
    int fallback_selected = -1;
    int fallback_rssi = -128;
    unsigned matched = 0;
    for (uint16_t i = 0; records && i < count; i++) {
      int profile = wifi_profile_for_ssid(records[i].ssid);
      if (profile < 0) continue;
      matched++;
      if (fallback_selected < 0 || records[i].rssi > fallback_rssi) {
        fallback_selected = profile;
        fallback_rssi = records[i].rssi;
      }
      if (profile == s_failed_wifi_profile) continue;
      if (selected < 0 || records[i].rssi > selected_rssi) {
        selected = profile;
        selected_rssi = records[i].rssi;
      }
    }
    free(records);
    /* Hidden APs cannot be discovered; fall back to the most recently saved. */
    if (selected < 0 && fallback_selected >= 0) {
      selected = fallback_selected;
      selected_rssi = fallback_rssi;
    }
    if (selected < 0) selected = 0;

    if ((s_idle_suspended && !s_idle_resuming) || s_online ||
        s_pending_credentials || s_pair_requested || !s_wifi_started) {
      continue;
    }

    wifi_config_t station = {0};
    strlcpy((char *)station.sta.ssid, s_wifi_profiles[selected].ssid,
            sizeof(station.sta.ssid));
    strlcpy((char *)station.sta.password, s_wifi_profiles[selected].password,
            sizeof(station.sta.password));
    station.sta.threshold.authmode = station.sta.password[0]
                                         ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    station.sta.pmf_cfg.capable = true;
    station.sta.pmf_cfg.required = false;
    strlcpy(s_saved_ssid, s_wifi_profiles[selected].ssid, sizeof(s_saved_ssid));
    strlcpy(s_saved_pass, s_wifi_profiles[selected].password, sizeof(s_saved_pass));
    s_active_wifi_profile = selected;
    esp_err_t error = esp_wifi_set_config(WIFI_IF_STA, &station);
    if (error == ESP_OK) error = esp_wifi_connect();
    s_wifi_connecting = error == ESP_OK;
    ESP_LOGI(TAG,
             "LY|WIFI_SELECT|saved=%u matched=%u selected=%s rssi=%d scan=%s connect=%s",
             (unsigned)s_wifi_profile_count, matched, s_saved_ssid, selected_rssi,
             esp_err_to_name(scan_error), esp_err_to_name(error));
    if (error != ESP_OK) wifi_retry_schedule();
  }
}

static esp_err_t save_binding(const char *token, const char *masked_account,
                              uint32_t binding_generation) {
  nvs_handle_t nvs;
  esp_err_t error = nvs_open("net", NVS_READWRITE, &nvs);
  if (error != ESP_OK) return error;
  error = nvs_set_str(nvs, "token", token);
  if (error == ESP_OK) error = nvs_set_str(nvs, "account", masked_account);
  if (error == ESP_OK) error = nvs_set_u32(nvs, "binding_gen", binding_generation);
  if (error == ESP_OK) error = nvs_commit(nvs);
  nvs_close(nvs);
  return error;
}

static void invalidate_binding_for_auth_repair(void) {
  nvs_handle_t nvs;
  if (nvs_open("net", NVS_READWRITE, &nvs) == ESP_OK) {
    nvs_erase_key(nvs, "token");
    nvs_erase_key(nvs, "account");
    /* Keep binding_gen until a successful claim proves whether this is the
       same account or a different owner.  A transient/expired-token 401 must
       never erase the durable agenda cache as a side effect of uploading. */
    nvs_commit(nvs);
    nvs_close(nvs);
  }
  memset(s_token, 0, sizeof(s_token));
  if (s_pair_lock) xSemaphoreTake(s_pair_lock, portMAX_DELAY);
  s_pair.masked_account[0] = '\0';
  if (s_pair_lock) xSemaphoreGive(s_pair_lock);
  s_bound = false;
  ESP_LOGW(TAG,
           "LY|PAIR|event=auth_invalidated generation=%lu agenda=preserved",
           (unsigned long)s_binding_generation);
}

static void auth_repair_binding(int http_status) {
  if (!provisioning_auth_repair_required(http_status)) return;
  invalidate_binding_for_auth_repair();

  /* Multiple cloud workers can observe the same revoked token.  Only the
     first one creates a challenge; later failures must not supersede a code
     that may already be visible on the device/account page. */
  bool started = false;
  if (s_pair_lock) xSemaphoreTake(s_pair_lock, portMAX_DELAY);
  if (!s_pair_requested) {
    random_digits(s_pair.pairing_code, 6);
    random_hex(s_pair_nonce, 16);
    s_pair_registered = false;
    s_pair_poll_ms = 1000;
    s_pair_requested = true;
    started = true;
  }
  if (s_pair_lock) xSemaphoreGive(s_pair_lock);
  if (!started) return;

  pair_set_state(NET_PAIR_WIFI_CONNECTED, ESP_OK, 0);
  ESP_LOGW(TAG,
           "LY|PAIR|event=auth_repair_required http=%d wifi_credentials=retained",
           http_status);
}

static void bootstrap_clock_from_candidate(int64_t candidate_epoch,
                                           const char *source) {
  int64_t local_epoch = (int64_t)time(NULL);
  if (!provisioning_clock_bootstrap_required(local_epoch, candidate_epoch)) return;
  time_t seconds = (time_t)candidate_epoch;
  if ((int64_t)seconds != candidate_epoch) return;
  struct timeval value = {.tv_sec = seconds, .tv_usec = 0};
  if (settimeofday(&value, NULL) != 0) {
    ESP_LOGW(TAG, "LY|TIME|event=clock_bootstrap source=%s result=set_failed",
             source ? source : "unknown");
    return;
  }
  esp_err_t rtc = rtc_sync_from_system();
  agenda_schedule_next();
  ESP_LOGI(TAG,
           "LY|TIME|event=clock_bootstrap source=%s epoch=%lld rtc=%s",
           source ? source : "unknown", (long long)candidate_epoch,
           esp_err_to_name(rtc));
  if (s_post) s_post(APP_EV_TIME_SYNC, 1);
}

static void bootstrap_clock_from_server(int64_t server_epoch,
                                        const char *source) {
  bootstrap_clock_from_candidate(server_epoch, source);
}

static bool cloud_transport_clock_ready(void) {
  return LUOYE_CFG_ALLOW_INSECURE_HTTP ||
         provisioning_https_clock_ready((int64_t)time(NULL));
}

static esp_err_t start_sta_candidate(const char *ssid, const char *password) {
  if (!provisioning_credentials_valid(ssid, password)) return ESP_ERR_INVALID_ARG;

  wifi_config_t config = {0};
  memcpy(config.sta.ssid, ssid, strlen(ssid));
  memcpy(config.sta.password, password, strlen(password));
  config.sta.threshold.authmode = strlen(password) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
  config.sta.pmf_cfg.capable = true;
  config.sta.pmf_cfg.required = false;

  strlcpy(s_pending_ssid, ssid, sizeof(s_pending_ssid));
  strlcpy(s_pending_pass, password, sizeof(s_pending_pass));
  s_pending_credentials = true;
  s_station_config_is_candidate = true;
  s_pending_attempts = s_online ? 0 : 1;

  esp_err_t error = esp_wifi_set_mode(WIFI_MODE_APSTA);
  if (error == ESP_OK) error = esp_wifi_set_config(WIFI_IF_STA, &config);
  if (error != ESP_OK) {
    s_pending_credentials = false;
    return error;
  }

  pair_set_state(NET_PAIR_WIFI_CONNECTING, ESP_OK, 0);
  if (s_online) {
    error = esp_wifi_disconnect();
  } else {
    error = esp_wifi_connect();
  }
  if (error != ESP_OK && error != ESP_ERR_WIFI_NOT_STARTED) {
    s_pending_credentials = false;
  }
  return error;
}

static esp_err_t portal_root(httpd_req_t *request) {
  httpd_resp_set_type(request, "text/html; charset=utf-8");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");
  return httpd_resp_send(request, PORTAL_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t portal_status(httpd_req_t *request) {
  net_pairing_info_t pair;
  pair_copy_locked(&pair);
  cJSON *root = cJSON_CreateObject();
  cJSON_AddStringToObject(root, "device_id", pair.device_id);
  cJSON_AddNumberToObject(root, "state", pair.state);
  cJSON_AddBoolToObject(root, "wifi_connected", s_online);
  cJSON_AddBoolToObject(root, "bound", s_bound);
  char *json = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!json) return httpd_resp_send_500(request);
  httpd_resp_set_type(request, "application/json");
  esp_err_t result = httpd_resp_sendstr(request, json);
  cJSON_free(json);
  return result;
}

static esp_err_t portal_networks(httpd_req_t *request) {
  wifi_scan_config_t scan = {.show_hidden = false};
  esp_err_t error = esp_wifi_scan_start(&scan, true);
  if (error != ESP_OK) return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "scan failed");

  uint16_t available = 0;
  esp_wifi_scan_get_ap_num(&available);
  if (available > 20) available = 20;
  wifi_ap_record_t *records = calloc(available ? available : 1, sizeof(*records));
  if (!records) return httpd_resp_send_500(request);
  uint16_t count = available;
  error = esp_wifi_scan_get_ap_records(&count, records);
  if (error != ESP_OK) {
    free(records);
    return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "scan read failed");
  }

  cJSON *root = cJSON_CreateObject();
  cJSON *networks = cJSON_AddArrayToObject(root, "networks");
  for (uint16_t i = 0; i < count; i++) {
    if (!records[i].ssid[0]) continue;
    bool duplicate = false;
    for (uint16_t j = 0; j < i; j++) {
      if (strcmp((const char *)records[i].ssid, (const char *)records[j].ssid) == 0) {
        duplicate = true;
        break;
      }
    }
    if (duplicate) continue;
    cJSON *network = cJSON_CreateObject();
    cJSON_AddStringToObject(network, "ssid", (const char *)records[i].ssid);
    cJSON_AddNumberToObject(network, "rssi", records[i].rssi);
    cJSON_AddBoolToObject(network, "secured", records[i].authmode != WIFI_AUTH_OPEN);
    cJSON_AddItemToArray(networks, network);
  }
  free(records);

  char *json = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!json) return httpd_resp_send_500(request);
  httpd_resp_set_type(request, "application/json");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");
  esp_err_t result = httpd_resp_sendstr(request, json);
  cJSON_free(json);
  return result;
}

static esp_err_t portal_provision(httpd_req_t *request) {
  if (request->content_len <= 0 || request->content_len > PORTAL_MAX_BODY) {
    return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "invalid request");
  }
  char body[PORTAL_MAX_BODY + 1];
  size_t received = 0;
  while (received < (size_t)request->content_len) {
    int count = httpd_req_recv(request, body + received,
                               (size_t)request->content_len - received);
    if (count <= 0) return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "receive failed");
    received += (size_t)count;
  }
  body[received] = '\0';

  char ssid[33] = {0};
  char password[65] = {0};
  if (!provisioning_form_value(body, "ssid", ssid, sizeof(ssid))) {
    return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "ssid required");
  }
  if (!provisioning_form_value(body, "password", password, sizeof(password))) {
    return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "invalid password");
  }
  if (!provisioning_credentials_valid(ssid, password)) {
    return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "invalid wifi credentials");
  }

  /* Browser time is an initial TLS bootstrap hint, not a continuing clock
     authority.  Missing/invalid values keep old browsers usable; the HTTPS
     gate below then waits for SNTP.  A valid RTC is never overwritten. */
  char client_time_text[24] = {0};
  int64_t client_epoch = 0;
  if (provisioning_form_value(body, "client_time_utc", client_time_text,
                              sizeof(client_time_text)) &&
      provisioning_parse_client_unix_utc(client_time_text, &client_epoch)) {
    bootstrap_clock_from_candidate(client_epoch, "softap_browser");
  }

  esp_err_t error = start_sta_candidate(ssid, password);
  memset(client_time_text, 0, sizeof(client_time_text));
  memset(password, 0, sizeof(password));
  memset(body, 0, sizeof(body));
  if (error != ESP_OK) {
    pair_set_state(NET_PAIR_ERROR, error, 0);
    return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "wifi start failed");
  }

  static const char RESPONSE[] =
    "<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<body style=\"font:18px system-ui;padding:30px;line-height:1.6;background:#f4f1ea\">"
    "<h2>正在验证 WiFi</h2><p>请查看录音卡墨水屏。连接成功后，断开 LUOYE 热点并恢复手机网络，再按照屏幕配对码登录 ClearMeeting 认领设备。</p>"
    "<p>账号密码不会保存到录音卡。</p></body>";
  httpd_resp_set_type(request, "text/html; charset=utf-8");
  return httpd_resp_send(request, RESPONSE, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t portal_redirect_404(httpd_req_t *request, httpd_err_code_t error) {
  (void)error;
  httpd_resp_set_status(request, "302 Found");
  httpd_resp_set_hdr(request, "Location", "http://192.168.4.1/");
  return httpd_resp_send(request, NULL, 0);
}

static esp_err_t portal_start(void) {
  if (s_portal) return ESP_OK;
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.max_uri_handlers = 8;
  config.stack_size = 6144;
  esp_err_t error = httpd_start(&s_portal, &config);
  if (error != ESP_OK) return error;

  const httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = portal_root};
  const httpd_uri_t status = {.uri = "/api/status", .method = HTTP_GET, .handler = portal_status};
  const httpd_uri_t networks = {.uri = "/api/networks", .method = HTTP_GET, .handler = portal_networks};
  const httpd_uri_t provision = {.uri = "/api/provision", .method = HTTP_POST, .handler = portal_provision};
  if ((error = httpd_register_uri_handler(s_portal, &root)) != ESP_OK ||
      (error = httpd_register_uri_handler(s_portal, &status)) != ESP_OK ||
      (error = httpd_register_uri_handler(s_portal, &networks)) != ESP_OK ||
      (error = httpd_register_uri_handler(s_portal, &provision)) != ESP_OK ||
      (error = httpd_register_err_handler(s_portal, HTTPD_404_NOT_FOUND,
                                          portal_redirect_404)) != ESP_OK) {
    httpd_stop(s_portal);
    s_portal = NULL;
  }
  return error;
}

static void portal_stop(void) {
  if (!s_portal) return;
  httpd_stop(s_portal);
  s_portal = NULL;
}

void net_enter_pairing(void) {
  if (!s_ap_netif) s_ap_netif = esp_netif_create_default_wifi_ap();
  if (!s_ap_netif) {
    pair_set_state(NET_PAIR_ERROR, ESP_ERR_NO_MEM, 0);
    return;
  }

  /* Provisioning AP is intentionally open: the user can join LUOYE-XXXX
     without entering a temporary password.  Account ownership is still
     protected by the separate one-time pairing code and device nonce. */
  s_pair.ap_password[0] = '\0';
  random_digits(s_pair.pairing_code, 6);
  random_hex(s_pair_nonce, 16);
  s_pair_registered = false;
  s_pair_poll_ms = 5000;
  if (!s_bound) s_pair.masked_account[0] = '\0';

  wifi_config_t ap = {0};
  strlcpy((char *)ap.ap.ssid, s_pair.ap_ssid, sizeof(ap.ap.ssid));
  ap.ap.ssid_len = strlen(s_pair.ap_ssid);
  ap.ap.channel = 1;
  ap.ap.max_connection = 4;
  ap.ap.authmode = WIFI_AUTH_OPEN;
  ap.ap.pmf_cfg.capable = false;
  ap.ap.pmf_cfg.required = false;

  esp_err_t error = esp_wifi_set_mode(WIFI_MODE_APSTA);
  if (error == ESP_OK) error = esp_wifi_set_config(WIFI_IF_AP, &ap);
  if (error == ESP_OK && !s_wifi_started) {
    error = esp_wifi_start();
    if (error == ESP_OK) s_wifi_started = true;
  }
  if (error == ESP_OK) error = portal_start();
  if (error != ESP_OK) {
    pair_set_state(NET_PAIR_ERROR, error, 0);
    return;
  }

  /* A bound device may enter pairing to rotate its token with the same account.
     The old token remains usable until pair/status atomically persists the new one. */
  s_pair_requested = true;
  pair_set_state(NET_PAIR_AP_READY, ESP_OK, 0);
  ESP_LOGI(TAG, "LY|PROVISION|state=ap_ready ssid=%s ip=192.168.4.1",
           s_pair.ap_ssid);
}

void net_exit_pairing(void) {
  portal_stop();
  s_pair_requested = false;
  s_pending_credentials = false;
  if (s_have_credentials) {
    if (s_station_config_is_candidate) {
      wifi_config_t saved = {0};
      strlcpy((char *)saved.sta.ssid, s_saved_ssid,
              sizeof(saved.sta.ssid));
      strlcpy((char *)saved.sta.password, s_saved_pass,
              sizeof(saved.sta.password));
      saved.sta.pmf_cfg.capable = true;
      esp_wifi_disconnect();
      esp_wifi_set_config(WIFI_IF_STA, &saved);
      s_station_config_is_candidate = false;
    }
    esp_wifi_set_mode(WIFI_MODE_STA);
    if (!s_online) esp_wifi_connect();
  } else if (s_wifi_started) {
    esp_wifi_stop();
    s_wifi_started = false;
  }
  pair_set_state(NET_PAIR_IDLE, ESP_OK, 0);
}

typedef struct {
  char data[RESPONSE_BYTES];
  size_t length;
  bool overflow;
} response_buffer_t;

static bool json_u32_value(cJSON *root, const char *name, uint32_t *out);

static esp_err_t pair_http_event(esp_http_client_event_t *event) {
  response_buffer_t *response = (response_buffer_t *)event->user_data;
  if (event->event_id == HTTP_EVENT_ON_DATA && response && event->data_len > 0) {
    size_t room = sizeof(response->data) - response->length - 1;
    size_t copy = (size_t)event->data_len < room ? (size_t)event->data_len : room;
    memcpy(response->data + response->length, event->data, copy);
    response->length += copy;
    response->data[response->length] = '\0';
    if (copy != (size_t)event->data_len) response->overflow = true;
  }
  return ESP_OK;
}

static esp_err_t anonymous_request(esp_http_client_method_t method,
                                   const char *path, const char *body,
                                   response_buffer_t *response,
                                   int *http_status) {
  if (!cloud_transport_clock_ready()) return ESP_ERR_INVALID_STATE;
  char url[256];
  int length = snprintf(url, sizeof(url), "%s%s", server_base_url(), path);
  if (length <= 0 || length >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;
  if (response) memset(response, 0, sizeof(*response));
  esp_http_client_config_t config = {
    .url = url,
    .method = method,
    .timeout_ms = 10000,
    .event_handler = response ? pair_http_event : NULL,
    .user_data = response,
    .crt_bundle_attach = LUOYE_CFG_ALLOW_INSECURE_HTTP
                           ? NULL : esp_crt_bundle_attach,
  };
  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (!client) return ESP_ERR_NO_MEM;
  esp_http_client_set_header(client, "X-Luoye-Protocol",
                             luoye_build_api_contract());
  esp_http_client_set_header(client, "X-Luoye-Firmware",
                             luoye_build_version());
  esp_http_client_set_header(client, "X-Luoye-Device", s_pair.device_id);
  if (body) {
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, strlen(body));
  }
  esp_err_t error = esp_http_client_perform(client);
  int status = esp_http_client_get_status_code(client);
  esp_http_client_cleanup(client);
  if (error == ESP_OK && response && response->overflow) {
    error = ESP_ERR_INVALID_SIZE;
  }
  if (http_status) *http_status = status;
  return error;
}

static bool capability_present(cJSON *array, const char *name) {
  if (!cJSON_IsArray(array) || !name) return false;
  cJSON *entry = NULL;
  cJSON_ArrayForEach(entry, array) {
    if (cJSON_IsString(entry) && strcmp(entry->valuestring, name) == 0) {
      return true;
    }
  }
  return false;
}

static bool firmware_at_least(const char *current, const char *minimum) {
  if (!current || !minimum) return false;
  unsigned long current_major = 0, current_minor = 0, current_patch = 0;
  unsigned long minimum_major = 0, minimum_minor = 0, minimum_patch = 0;
  int current_end = 0, minimum_end = 0;
  if (sscanf(current, "%lu.%lu.%lu%n", &current_major, &current_minor,
             &current_patch, &current_end) != 3 ||
      sscanf(minimum, "%lu.%lu.%lu%n", &minimum_major, &minimum_minor,
             &minimum_patch, &minimum_end) != 3 ||
      (current[current_end] && current[current_end] != '-' &&
       current[current_end] != '+') ||
      (minimum[minimum_end] && minimum[minimum_end] != '-' &&
       minimum[minimum_end] != '+')) return false;
  if (current_major != minimum_major) return current_major > minimum_major;
  if (current_minor != minimum_minor) return current_minor > minimum_minor;
  return current_patch >= minimum_patch;
}

static esp_err_t build_info_exchange(void) {
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = anonymous_request(HTTP_METHOD_GET, BUILD_INFO_PATH, NULL,
                                      &response, &status);
  if (error != ESP_OK || status != 200) {
    s_contract_checked = false;
    cloud_set_ready(false);
    pair_set_state(NET_PAIR_ERROR, error == ESP_OK ? ESP_FAIL : error, status);
    return error == ESP_OK ? ESP_FAIL : error;
  }
  cJSON *root = cJSON_Parse(response.data);
  cJSON *contract = root ? cJSON_GetObjectItemCaseSensitive(root, "api_contract") : NULL;
  cJSON *minimum = root ? cJSON_GetObjectItemCaseSensitive(root,
                                                            "minimum_firmware") : NULL;
  cJSON *auth_profile = root
                          ? cJSON_GetObjectItemCaseSensitive(
                                root, "device_auth_profile") : NULL;
  cJSON *capabilities = root ? cJSON_GetObjectItemCaseSensitive(root, "capabilities") : NULL;
  static const char *const REQUIRED[] = {
    "device_pairing", "idempotent_upload", "session_state",
    "agenda_sync", "voice_todo", "storage_management",
    "network_scheduler", "bulk_upload_10mib", "range_repair",
    "streaming_request_body", "session_cancel", "live_epoch_resume",
    "manual_gap_repair", "independent_sd_delete",
    "transcript_only_live_v1", "canonical_offline_diarization_v2",
  };
  bool valid = cJSON_IsString(contract) &&
               strcmp(contract->valuestring, luoye_build_api_contract()) == 0 &&
               cJSON_IsString(minimum) &&
               firmware_at_least(luoye_build_version(), minimum->valuestring) &&
               cJSON_IsString(auth_profile) &&
               strcmp(auth_profile->valuestring,
                      luoye_build_device_auth_profile()) == 0 &&
               cJSON_IsArray(capabilities);
  for (size_t i = 0; valid && i < sizeof(REQUIRED) / sizeof(REQUIRED[0]); ++i) {
    valid = capability_present(capabilities, REQUIRED[i]);
  }
  cJSON_Delete(root);
  if (!valid) {
    s_contract_checked = false;
    cloud_set_ready(false);
    pair_set_state(NET_PAIR_ERROR, ESP_ERR_NOT_SUPPORTED, status);
    ESP_LOGE(TAG, "LY|COMPAT|result=rejected expected=%s",
             luoye_build_api_contract());
    return ESP_ERR_NOT_SUPPORTED;
  }
  s_contract_checked = true;
  cloud_set_ready(true);
  ESP_LOGI(TAG,
           "LY|COMPAT|result=accepted api=%s auth_profile=%s transport=%s",
           luoye_build_api_contract(),
           luoye_build_device_auth_profile(),
           LUOYE_CFG_ALLOW_INSECURE_HTTP ? "http-engineering" : "https");
  return ESP_OK;
}

static bool pair_response_state(const response_buffer_t *response,
                                const char **state_out, uint32_t *poll_ms) {
  cJSON *root = cJSON_Parse(response ? response->data : NULL);
  cJSON *binding = root ? cJSON_GetObjectItemCaseSensitive(root,
                                                           "binding_status") : NULL;
  cJSON *poll = root ? cJSON_GetObjectItemCaseSensitive(root,
                                                        "poll_after_seconds") : NULL;
  bool valid = cJSON_IsString(binding) &&
      (strcmp(binding->valuestring, "pending") == 0 ||
       strcmp(binding->valuestring, "bound") == 0);
  static char state[9];
  if (valid) snprintf(state, sizeof(state), "%s", binding->valuestring);
  if (valid && cJSON_IsNumber(poll) && poll->valuedouble >= 1 &&
      poll->valuedouble <= 60) {
    *poll_ms = (uint32_t)poll->valuedouble * 1000U;
  }
  cJSON_Delete(root);
  if (valid && state_out) *state_out = state;
  return valid;
}

static bool response_error_code(const response_buffer_t *response,
                                char *out, size_t out_size) {
  if (!response || !out || out_size == 0) return false;
  out[0] = '\0';
  cJSON *root = cJSON_Parse(response->data);
  cJSON *error = root ? cJSON_GetObjectItemCaseSensitive(root, "error") : NULL;
  cJSON *code = cJSON_IsObject(error)
                  ? cJSON_GetObjectItemCaseSensitive(error, "code") : NULL;
  bool valid = cJSON_IsString(code) && code->valuestring[0] &&
               strlen(code->valuestring) < out_size;
  if (valid) strlcpy(out, code->valuestring, out_size);
  cJSON_Delete(root);
  return valid;
}

static void pair_restart_challenge(int http_status, const char *error_code) {
  char pairing_code[sizeof(s_pair.pairing_code)];
  random_digits(pairing_code, 6);
  random_hex(s_pair_nonce, 16);
  pair_set_text(pairing_code, NULL);
  s_pair_registered = false;
  s_pair_poll_ms = 1000;
  pair_set_state(NET_PAIR_WIFI_CONNECTED, ESP_OK, 0);
  ESP_LOGW(TAG, "LY|PAIR|event=challenge_restart http=%d code=%s",
           http_status, error_code);
}

static esp_err_t pair_start_exchange(void) {
  cJSON *request = cJSON_CreateObject();
  if (!request) return ESP_ERR_NO_MEM;
  cJSON_AddStringToObject(request, "device_id", s_pair.device_id);
  cJSON_AddStringToObject(request, "pairing_code", s_pair.pairing_code);
  cJSON_AddStringToObject(request, "nonce", s_pair_nonce);
  cJSON_AddStringToObject(request, "firmware_version", luoye_build_version());
  cJSON_AddStringToObject(request, "hardware_revision", luoye_build_hardware_rev());
  cJSON_AddStringToObject(request, "protocol_version", luoye_build_api_contract());
  cJSON *capabilities = cJSON_AddArrayToObject(request, "capabilities");
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("fixed_sd"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("pdm_stereo"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("offline_upload"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("agenda"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("voice_todo"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("storage_management"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("network_scheduler"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("bulk_upload_10mib"));
  cJSON_AddItemToArray(capabilities, cJSON_CreateString("range_repair"));
  cJSON_AddItemToArray(capabilities,
                       cJSON_CreateString("transcript_only_live_v1"));
  char *body = cJSON_PrintUnformatted(request);
  cJSON_Delete(request);
  if (!body) return ESP_ERR_NO_MEM;

  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = anonymous_request(HTTP_METHOD_POST, PAIR_START_PATH, body,
                                      &response, &status);
  cJSON_free(body);

  if (error != ESP_OK || status < 200 || status >= 300) {
    char error_code[40];
    if (error == ESP_OK && response_error_code(&response, error_code,
                                                sizeof(error_code)) &&
        provisioning_pair_restart_required(status, error_code)) {
      pair_restart_challenge(status, error_code);
      return ESP_OK;
    }
    pair_set_state(NET_PAIR_ERROR, error == ESP_OK ? ESP_FAIL : error, status);
    return error == ESP_OK ? ESP_FAIL : error;
  }
  const char *state = NULL;
  if (!pair_response_state(&response, &state, &s_pair_poll_ms)) {
    pair_set_state(NET_PAIR_ERROR, ESP_ERR_INVALID_RESPONSE, status);
    return ESP_ERR_INVALID_RESPONSE;
  }
  (void)state;
  s_pair_registered = true;
  pair_set_state(NET_PAIR_CLAIM_PENDING, ESP_OK, status);
  return ESP_OK;
}

static esp_err_t pair_status_exchange(void) {
  cJSON *request = cJSON_CreateObject();
  if (!request) return ESP_ERR_NO_MEM;
  cJSON_AddStringToObject(request, "device_id", s_pair.device_id);
  cJSON_AddStringToObject(request, "nonce", s_pair_nonce);
  char *body = cJSON_PrintUnformatted(request);
  cJSON_Delete(request);
  if (!body) return ESP_ERR_NO_MEM;
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = anonymous_request(HTTP_METHOD_POST, PAIR_STATUS_PATH, body,
                                      &response, &status);
  cJSON_free(body);
  if (error != ESP_OK || status < 200 || status >= 300) {
    char error_code[40];
    if (error == ESP_OK && response_error_code(&response, error_code,
                                                sizeof(error_code)) &&
        provisioning_pair_restart_required(status, error_code)) {
      pair_restart_challenge(status, error_code);
      return ESP_OK;
    }
    pair_set_state(NET_PAIR_ERROR, error == ESP_OK ? ESP_FAIL : error, status);
    return error == ESP_OK ? ESP_FAIL : error;
  }

  cJSON *root = cJSON_Parse(response.data);
  cJSON *binding = root ? cJSON_GetObjectItemCaseSensitive(root,
                                                           "binding_status") : NULL;
  cJSON *poll = root ? cJSON_GetObjectItemCaseSensitive(root,
                                                        "poll_after_seconds") : NULL;
  const char *binding_text = cJSON_IsString(binding) ? binding->valuestring : "";
  if (strcmp(binding_text, "pending") == 0) {
    if (cJSON_IsNumber(poll) && poll->valuedouble >= 1 &&
        poll->valuedouble <= 60) {
      s_pair_poll_ms = (uint32_t)poll->valuedouble * 1000U;
    }
    cJSON_Delete(root);
    pair_set_state(NET_PAIR_CLAIM_PENDING, ESP_OK, status);
    return ESP_OK;
  }
  cJSON *token = root ? cJSON_GetObjectItemCaseSensitive(root, "device_token") : NULL;
  cJSON *account = root ? cJSON_GetObjectItemCaseSensitive(root, "masked_account") : NULL;
  cJSON *server_time = root
                         ? cJSON_GetObjectItemCaseSensitive(root, "server_time") : NULL;
  int64_t server_epoch = 0;
  bool server_time_valid = cJSON_IsString(server_time) &&
      provisioning_parse_utc_iso8601(server_time->valuestring, &server_epoch);
  uint32_t binding_generation = 0;
  const char *account_text = cJSON_IsString(account) ? account->valuestring : "";
  if (strcmp(binding_text, "bound") == 0 && cJSON_IsString(token) &&
      token->valuestring[0] &&
      strlen(token->valuestring) < sizeof(s_token) &&
      account_text[0] &&
      strlen(account_text) < sizeof(s_pair.masked_account) &&
      json_u32_value(root, "binding_generation", &binding_generation) &&
      binding_generation > 0 &&
      (s_binding_generation == 0 || binding_generation >= s_binding_generation)) {
    error = save_binding(token->valuestring, account_text, binding_generation);
    if (error == ESP_OK) {
      strlcpy(s_token, token->valuestring, sizeof(s_token));
      s_bound = true;
      s_binding_generation = binding_generation;
      agenda_reset_binding(binding_generation);
      s_agenda_sync_requested = true;
      s_pair_requested = false;
      s_pair_registered = false;
      memset(s_pair_nonce, 0, sizeof(s_pair_nonce));
      portal_stop();
      esp_err_t mode_result = esp_wifi_set_mode(WIFI_MODE_STA);
      if (mode_result != ESP_OK) {
        ESP_LOGW(TAG, "LY|PAIR|event=transport_close result=%s",
                 esp_err_to_name(mode_result));
      }
      pair_set_text(NULL, account_text);
      pair_set_state(NET_PAIR_BOUND, ESP_OK, status);
      cloud_set_ready(true);
      if (server_time_valid) bootstrap_clock_from_server(server_epoch, "pair");
    } else {
      pair_set_state(NET_PAIR_ERROR, error, status);
    }
  } else {
    pair_set_state(NET_PAIR_ERROR, ESP_ERR_INVALID_RESPONSE, status);
    error = ESP_ERR_INVALID_RESPONSE;
  }
  cJSON_Delete(root);
  return error;
}

static void pairing_task(void *argument) {
  (void)argument;
  for (;;) {
    uint32_t delay = s_pair_poll_ms;
    if (s_online && (!s_idle_suspended || s_idle_agenda_maintenance) &&
        !s_contract_checked) {
      if (cloud_transport_clock_ready()) {
        s_pairing_busy = true;
        build_info_exchange();
        s_pairing_busy = false;
      }
      delay = cloud_transport_clock_ready() ? 5000 : 1000;
    } else if (s_online && !s_idle_suspended && s_pair_requested) {
      s_pairing_busy = true;
      if (s_pair_registered) pair_status_exchange();
      else pair_start_exchange();
      s_pairing_busy = false;
      delay = s_pair_poll_ms;
    }
    vTaskDelay(pdMS_TO_TICKS(delay < 1000 ? 1000 : delay));
  }
}

void net_session_begin(const char *session_id, app_scene_t scene, const char *title) {
  (void)scene;
  (void)title;
  esp_err_t error = sd_upload_assign_identity(
      session_id, s_pair.device_id, s_bound ? s_binding_generation : 0);
  if (error == ESP_OK && s_bound) {
    strlcpy(s_live_session_id, session_id, sizeof(s_live_session_id));
    if (!s_cloud_ready) s_live_gap_signal = true;
  }
  live_reset(session_id);
  ESP_LOGI(TAG, "LY|UPLOAD|event=session_queued id=%s binding=%lu result=%s",
           session_id, (unsigned long)(s_bound ? s_binding_generation : 0),
           esp_err_to_name(error));
}

static void set_authorization(esp_http_client_handle_t client) {
  if (!s_token[0]) return;
  char header[sizeof(s_token) + 8];
  snprintf(header, sizeof(header), "Bearer %s", s_token);
  esp_http_client_set_header(client, "Authorization", header);
  memset(header, 0, sizeof(header));
}

static void sha256_hex(const void *data, size_t size, char out[65]) {
  static const char HEX[] = "0123456789abcdef";
  unsigned char digest[32];
  mbedtls_sha256((const unsigned char *)data, size, digest, 0);
  for (size_t i = 0; i < sizeof(digest); ++i) {
    out[i * 2] = HEX[digest[i] >> 4];
    out[i * 2 + 1] = HEX[digest[i] & 0x0f];
  }
  out[64] = '\0';
}

static esp_err_t cloud_request(esp_http_client_method_t method,
                               const char *url, const char *content_type,
                               const char *idempotency_key,
                               const void *body, size_t body_size,
                               const luoye_upload_chunk_t *chunk,
                               const char *sha256,
                               response_buffer_t *response, int *http_status) {
  if (!cloud_transport_clock_ready()) return ESP_ERR_INVALID_STATE;
  if (chunk && ((chunk->offset & 1U) || (chunk->length & 1U))) {
    return ESP_ERR_INVALID_ARG;
  }
  if (response) memset(response, 0, sizeof(*response));
  esp_http_client_config_t config = {
    .url = url,
    .method = method,
    .timeout_ms = 20000,
    .buffer_size = 2048,
    .buffer_size_tx = HTTP_TX_BUFFER_BYTES,
    .event_handler = response ? pair_http_event : NULL,
    .user_data = response,
    .crt_bundle_attach = LUOYE_CFG_ALLOW_INSECURE_HTTP
                           ? NULL : esp_crt_bundle_attach,
  };
  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (!client) return ESP_ERR_NO_MEM;
  if (content_type) esp_http_client_set_header(client, "Content-Type", content_type);
  esp_http_client_set_header(client, "X-Luoye-Protocol", luoye_build_api_contract());
  esp_http_client_set_header(client, "X-Luoye-Firmware", luoye_build_version());
  esp_http_client_set_header(client, "X-Luoye-Device", s_pair.device_id);
  if (idempotency_key) {
    esp_http_client_set_header(client, "Idempotency-Key", idempotency_key);
  }
  char byte_offset[16], byte_count[16];
  if (chunk) {
    snprintf(byte_offset, sizeof(byte_offset), "%lu",
             (unsigned long)chunk->offset);
    snprintf(byte_count, sizeof(byte_count), "%lu",
             (unsigned long)chunk->length);
    esp_http_client_set_header(client, "X-Byte-Offset", byte_offset);
    esp_http_client_set_header(client, "X-Byte-Count", byte_count);
  }
  if (sha256) esp_http_client_set_header(client, "X-Content-SHA256", sha256);
  set_authorization(client);
  if (body || body_size) {
    esp_http_client_set_post_field(client, body ? (const char *)body : "", body_size);
  }
  esp_err_t error = esp_http_client_perform(client);
  int status = esp_http_client_get_status_code(client);
  esp_http_client_cleanup(client);
  if (error == ESP_OK && response && response->overflow) {
    error = ESP_ERR_INVALID_SIZE;
  }
  if (http_status) *http_status = status;
  return error;
}

static uint32_t retry_item(sd_upload_item_t *item, esp_err_t error,
                           int http_status, const char *phase) {
  if (storage_sd_faulted()) {
    stop_for_storage_fault(phase, error);
    return 30000;
  }
  luoye_upload_http_class_t classification =
      luoye_upload_classify_http(error == ESP_OK, http_status);
  item->last_http_status = http_status;
  if (classification == LUOYE_UPLOAD_HTTP_AUTH) {
    snprintf(item->state, sizeof(item->state), "auth_blocked");
    sd_upload_save(item);
    auth_repair_binding(http_status);
    ESP_LOGW(TAG, "LY|UPLOAD|id=%s phase=%s result=auth_blocked http=%d",
             item->session_id, phase, http_status);
    return 30000;
  }
  if (classification == LUOYE_UPLOAD_HTTP_CONFLICT) {
    snprintf(item->state, sizeof(item->state), "permanent_error");
    sd_upload_save(item);
    ESP_LOGE(TAG, "LY|UPLOAD|id=%s phase=%s result=conflict http=%d",
             item->session_id, phase, http_status);
    return 30000;
  }
  if (classification == LUOYE_UPLOAD_HTTP_PERMANENT) {
    snprintf(item->state, sizeof(item->state), "permanent_error");
    sd_upload_save(item);
    ESP_LOGE(TAG, "LY|UPLOAD|id=%s phase=%s result=permanent http=%d",
             item->session_id, phase, http_status);
    return 30000;
  }
  if (!item->local_closed && strcmp(item->upload_mode, "live") == 0) {
    if (!item->live_resume_required) {
      item->gap_start_bytes = item->acknowledged_bytes;
    }
    item->live_resume_required = true;
    ESP_LOGW(TAG,
             "LY|LIVE_GAP|id=%s state=detected start=%lu phase=%s",
             item->session_id, (unsigned long)item->gap_start_bytes, phase);
  }
  item->retry_count++;
  snprintf(item->state, sizeof(item->state), "upload_retry");
  sd_upload_save(item);
  uint32_t delay = luoye_upload_retry_delay_ms(item->retry_count, esp_random());
  ESP_LOGW(TAG,
           "LY|UPLOAD|id=%s phase=%s result=retry attempt=%lu err=%s http=%d delay_ms=%lu",
           item->session_id, phase, (unsigned long)item->retry_count,
           esp_err_to_name(error), http_status, (unsigned long)delay);
  return delay;
}

static void report_uploader_storage_errno(const char *source, int value) {
  if (value == EIO || value == ENODEV || value == ENXIO ||
      value == ETIMEDOUT) {
    storage_sd_report_io_fault(source, ESP_FAIL, value);
  }
}

static uint32_t save_stage(sd_upload_item_t *item, const char *state) {
  snprintf(item->state, sizeof(item->state), "%s", state);
  item->retry_count = 0;
  item->last_http_status = 0;
  if (sd_upload_save(item) != ESP_OK) {
    return retry_item(item, ESP_FAIL, 0, "persist");
  }
  return 0;
}

static void mark_live_gap(sd_upload_item_t *item, const char *reason) {
  if (!item || strcmp(item->upload_mode, "live") != 0 ||
      item->final_acked) return;
  if (!item->live_resume_required) {
    item->gap_start_bytes = item->acknowledged_bytes;
  }
  item->live_resume_required = true;
  if (item->local_closed && item->pcm_bytes > item->gap_start_bytes) {
    item->deferred_gaps = true;
  }
  if (sd_upload_save(item) == ESP_OK) {
    ESP_LOGW(TAG,
             "LY|LIVE_GAP|id=%s state=queued start=%lu current=%lu closed=%d reason=%s",
             item->session_id, (unsigned long)item->gap_start_bytes,
             (unsigned long)item->pcm_bytes, item->local_closed,
             reason ? reason : "network");
  }
}

static bool json_add_utc_or_null(cJSON *root, const char *name,
                                 int64_t epoch_seconds) {
  if (!root || !name) return false;
  if (epoch_seconds <= 0) return cJSON_AddNullToObject(root, name) != NULL;
  time_t epoch = (time_t)epoch_seconds;
  if ((int64_t)epoch != epoch_seconds) return false;
  struct tm utc = {0};
  char text[24];
  if (!gmtime_r(&epoch, &utc) ||
      strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &utc) != 20) {
    return false;
  }
  return cJSON_AddStringToObject(root, name, text) != NULL;
}

static uint32_t create_remote_session(sd_upload_item_t *item) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "create_json");
  const char *scene = strcmp(item->scene, "translate") == 0
                        ? "translate" : "meeting";
  cJSON_AddStringToObject(root, "client_session_id", item->session_id);
  if (!json_add_utc_or_null(root, "started_at_utc", item->started_at_utc)) {
    cJSON_Delete(root);
    return retry_item(item, ESP_ERR_INVALID_ARG, 0, "create_time");
  }
  cJSON_AddStringToObject(root, "scene", scene);
  cJSON_AddStringToObject(root, "upload_mode",
                          item->upload_mode[0] ? item->upload_mode : "live");
  cJSON_AddStringToObject(root, "title", item->title);
  cJSON_AddStringToObject(root, "source_language", "auto");
  if (strcmp(scene, "translate") == 0) {
    cJSON_AddStringToObject(root, "target_language", "zh-CN");
  } else {
    cJSON_AddNullToObject(root, "target_language");
  }
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  cJSON *audio = cJSON_AddObjectToObject(root, "audio");
  cJSON_AddStringToObject(audio, "codec", "pcm_s16le");
  cJSON_AddNumberToObject(audio, "sample_rate", 16000);
  cJSON_AddNumberToObject(audio, "channels", 1);
  cJSON_AddNumberToObject(audio, "bits_per_sample", 16);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "create_json");

  char url[240], key[192];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions", server_base_url());
  if (!luoye_upload_create_key(key, sizeof(key), item->session_id)) {
    cJSON_free(body);
    return retry_item(item, ESP_ERR_INVALID_SIZE, 0, "create_key");
  }
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json",
      key, body, strlen(body), NULL, NULL, &response, &status);
  cJSON_free(body);
  luoye_upload_http_class_t classification =
      luoye_upload_classify_http(error == ESP_OK, status);
  if (classification != LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "create");
  }

  cJSON *reply = cJSON_Parse(response.data);
  cJSON *server_id = reply ? cJSON_GetObjectItemCaseSensitive(reply, "server_session_id") : NULL;
  uint32_t next = 0, received_chunks = 0, received_samples = 0;
  uint32_t acknowledged = 0, acknowledged_reply = 0;
  bool live_cursor = json_u32_value(reply, "live_next_seq", &next) &&
      json_u32_value(reply, "live_acknowledged_bytes", &acknowledged) &&
      acknowledged <= item->pcm_bytes;
  bool legacy_cursor = !live_cursor &&
      json_u32_value(reply, "next_seq", &next) &&
      json_u32_value(reply, "received_chunks", &received_chunks) &&
      received_chunks == next &&
      json_u32_value(reply, "received_samples", &received_samples) &&
      luoye_upload_progress_from_samples(item->pcm_bytes, CHUNK_BYTES,
                                         next, received_samples,
                                         &acknowledged) &&
      json_u32_value(reply, "acknowledged_bytes", &acknowledged_reply) &&
      acknowledged_reply == acknowledged;
  bool valid = cJSON_IsString(server_id) && server_id->valuestring[0] &&
               strlen(server_id->valuestring) < sizeof(item->server_session_id) &&
               luoye_upload_safe_path_id(server_id->valuestring) &&
               (live_cursor || legacy_cursor);
  if (!valid) {
    cJSON_Delete(reply);
    return retry_item(item, ESP_ERR_INVALID_RESPONSE, status, "create_response");
  }
  snprintf(item->server_session_id, sizeof(item->server_session_id), "%s",
           server_id->valuestring);
  item->remote_session_created = true;
  item->next_seq = next;
  item->acknowledged_bytes = acknowledged;
  cJSON_Delete(reply);
  uint32_t delay = save_stage(item, "uploading");
  if (!delay) {
    ESP_LOGI(TAG, "LY|UPLOAD|id=%s phase=create result=acked offset=%lu seq=%lu",
             item->session_id, (unsigned long)item->acknowledged_bytes,
             (unsigned long)item->next_seq);
  }
  return delay ? delay : 20;
}

static uint32_t resume_live_epoch(sd_upload_item_t *item) {
  uint32_t resume_offset = item->pcm_bytes;
  if (resume_offset < item->gap_start_bytes) {
    return retry_item(item, ESP_ERR_INVALID_STATE, 0, "live_resume_cursor");
  }
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "live_resume_json");
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  cJSON_AddNumberToObject(root, "gap_start_bytes", item->gap_start_bytes);
  cJSON_AddNumberToObject(root, "resume_offset_bytes", resume_offset);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "live_resume_json");
  char url[300], key[192];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/live-resume",
           server_base_url(), item->server_session_id);
  snprintf(key, sizeof(key), "live-resume:%s:%lu:%lu", item->session_id,
           (unsigned long)item->gap_start_bytes, (unsigned long)resume_offset);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json",
                                  key, body, strlen(body), NULL, NULL,
                                  &response, &status);
  cJSON_free(body);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "live_resume");
  }
  cJSON *reply = cJSON_Parse(response.data);
  uint32_t next = 0, acknowledged = 0, gap_end = 0;
  cJSON *resumed = reply ? cJSON_GetObjectItemCaseSensitive(reply, "resumed") : NULL;
  cJSON *gap_pending = reply
                         ? cJSON_GetObjectItemCaseSensitive(reply, "gap_pending") : NULL;
  bool valid = cJSON_IsTrue(resumed) &&
      json_u32_value(reply, "live_next_seq", &next) &&
      json_u32_value(reply, "live_acknowledged_bytes", &acknowledged) &&
      json_u32_value(reply, "gap_end_bytes", &gap_end) &&
      acknowledged == resume_offset && gap_end == resume_offset;
  bool pending = cJSON_IsTrue(gap_pending);
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status,
                                "live_resume_ack");
  item->next_seq = next;
  item->acknowledged_bytes = acknowledged;
  item->live_resume_required = false;
  item->deferred_gaps = item->deferred_gaps || pending;
  item->gap_start_bytes = acknowledged;
  uint32_t delay = save_stage(item, "uploading");
  if (!delay) {
    ESP_LOGI(TAG,
             "LY|LIVE_GAP|id=%s state=realtime_resumed offset=%lu seq=%lu deferred=%d",
             item->session_id, (unsigned long)acknowledged,
             (unsigned long)next, item->deferred_gaps);
  }
  return delay ? delay : 20;
}

static uint32_t upload_one_chunk(sd_upload_item_t *item, uint8_t *buffer) {
  uint32_t remaining = item->pcm_bytes - item->acknowledged_bytes;
  if (!item->local_closed && remaining < CHUNK_BYTES) return 0;
  luoye_upload_chunk_t chunk = {
    .seq = item->next_seq,
    .offset = item->acknowledged_bytes,
    .length = remaining > CHUNK_BYTES ? CHUNK_BYTES : remaining,
  };
  if (!chunk.length) return 0;
  size_t received = 0;
  esp_err_t error = sd_upload_read_audio(item, chunk.offset, buffer,
                                         chunk.length, &received);
  if (error != ESP_OK || received != chunk.length) {
    return retry_item(item, error == ESP_OK ? ESP_FAIL : error, 0, "read_audio");
  }
  char hash[65], key[192], url[280];
  sha256_hex(buffer, received, hash);
  if (!luoye_upload_chunk_key(key, sizeof(key), item->session_id,
                              chunk.seq, hash)) {
    return retry_item(item, ESP_ERR_INVALID_SIZE, 0, "chunk_key");
  }
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/audio/%lu",
           server_base_url(), item->server_session_id, (unsigned long)chunk.seq);
  response_buffer_t response = {0};
  int status = 0;
  error = cloud_request(HTTP_METHOD_PUT, url,
                         "audio/L16;rate=16000;channels=1", key,
                         buffer, received, &chunk, hash, &response, &status);
  luoye_upload_http_class_t classification =
      luoye_upload_classify_http(error == ESP_OK, status);
  if (classification != LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "audio");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *accepted = reply ? cJSON_GetObjectItemCaseSensitive(reply, "accepted") : NULL;
  cJSON *duplicate = reply ? cJSON_GetObjectItemCaseSensitive(reply, "duplicate") : NULL;
  uint32_t reply_seq = 0, next = 0, acknowledged = 0;
  bool valid = (cJSON_IsTrue(accepted) || cJSON_IsTrue(duplicate)) &&
      json_u32_value(reply, "seq", &reply_seq) && reply_seq == chunk.seq &&
      json_u32_value(reply, "live_next_seq", &next) &&
      json_u32_value(reply, "live_acknowledged_bytes", &acknowledged) &&
      next == chunk.seq + 1U &&
      acknowledged == chunk.offset + chunk.length;
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status, "audio_ack");
  item->next_seq = next;
  item->acknowledged_bytes = acknowledged;
  uint32_t delay = save_stage(item, "uploading");
  if (!delay) {
    ESP_LOGI(TAG, "LY|UPLOAD|id=%s phase=audio seq=%lu offset=%lu bytes=%lu lag_bytes=%lu result=acked",
             item->session_id, (unsigned long)chunk.seq,
             (unsigned long)chunk.offset, (unsigned long)chunk.length,
             (unsigned long)(item->pcm_bytes - item->acknowledged_bytes));
  }
  return delay ? delay : 20;
}

typedef struct {
  uint32_t offset;
  uint32_t length;
  uint32_t covered_bytes;
  bool complete;
} range_plan_t;

typedef struct {
  uint64_t total_us;
  uint64_t sd_read_us;
  uint64_t sha_us;
  uint32_t read_calls;
  uint32_t bytes_read;
  bool precomputed;
} range_hash_diag_t;

typedef struct {
  uint64_t total_us;
  uint64_t connect_us;
  uint64_t sd_read_us;
  uint64_t write_us;
  uint64_t response_us;
  uint32_t read_calls;
  uint32_t write_calls;
  uint32_t bytes_read;
  uint32_t bytes_written;
} range_http_diag_t;

static unsigned long long diag_ms(uint64_t microseconds) {
  return (unsigned long long)((microseconds + 500U) / 1000U);
}

static unsigned long long diag_bytes_per_second(uint32_t bytes,
                                                uint64_t microseconds) {
  if (!bytes || !microseconds) return 0;
  return (unsigned long long)(((uint64_t)bytes * 1000000ULL) / microseconds);
}

static void digest_hex(const unsigned char digest[32], char out[65]) {
  static const char HEX[] = "0123456789abcdef";
  for (size_t i = 0; i < 32; ++i) {
    out[i * 2] = HEX[digest[i] >> 4];
    out[i * 2 + 1] = HEX[digest[i] & 0x0f];
  }
  out[64] = '\0';
}

static esp_err_t hash_audio_range(const sd_upload_item_t *item,
                                  uint32_t offset, uint32_t length,
                                  uint8_t *scratch, size_t scratch_size,
                                  char hash[65],
                                  range_hash_diag_t *diag) {
  int64_t total_started_us = esp_timer_get_time();
  if (diag) memset(diag, 0, sizeof(*diag));
  if (!item || !scratch || scratch_size < 4096 || !length ||
      offset > item->pcm_bytes || length > item->pcm_bytes - offset) {
    return ESP_ERR_INVALID_ARG;
  }
  esp_err_t cached = sd_upload_range_sha256(item, offset, length, hash);
  if (cached == ESP_OK) {
    if (diag) {
      diag->precomputed = true;
      diag->total_us = (uint64_t)(esp_timer_get_time() - total_started_us);
    }
    return ESP_OK;
  }
  if (cached != ESP_ERR_NOT_FOUND) {
    ESP_LOGW(TAG,
             "LY|UPLOAD_DIAG|id=%s event=range_hash_cache result=%s fallback=scan",
             item->session_id, esp_err_to_name(cached));
    if (storage_sd_faulted()) return cached;
  }
  if (!storage_sd_mounted()) return ESP_ERR_INVALID_STATE;
  char path[SD_UPLOAD_DIR_BYTES + 16];
  snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
  FILE *file = fopen(path, "rb");
  if (!file) {
    int open_errno = errno;
    report_uploader_storage_errno("range_audio_open", open_errno);
    return open_errno == ENOENT ? ESP_ERR_NOT_FOUND : ESP_FAIL;
  }
  esp_err_t result = fseek(file, 44L + (long)offset, SEEK_SET) == 0
                       ? ESP_OK : ESP_FAIL;
  if (result != ESP_OK) {
    report_uploader_storage_errno("range_audio_seek", errno);
  }
  mbedtls_sha256_context context;
  unsigned char digest[32];
  mbedtls_sha256_init(&context);
  if (result == ESP_OK) {
    int64_t started_us = esp_timer_get_time();
    if (mbedtls_sha256_starts(&context, false) != 0) result = ESP_FAIL;
    if (diag) diag->sha_us += (uint64_t)(esp_timer_get_time() - started_us);
  }
  uint32_t remaining = length;
  while (result == ESP_OK && remaining) {
    size_t wanted = remaining < scratch_size ? remaining : scratch_size;
    int64_t read_started_us = esp_timer_get_time();
    size_t read = 0;
    esp_err_t read_result = storage_sd_read(file, scratch, wanted, &read);
    if (diag) {
      diag->sd_read_us += (uint64_t)(esp_timer_get_time() - read_started_us);
      diag->read_calls++;
      diag->bytes_read += (uint32_t)read;
    }
    if (read_result != ESP_OK || read != wanted) {
      result = ESP_FAIL;
      break;
    }
    int64_t sha_started_us = esp_timer_get_time();
    int sha_result = mbedtls_sha256_update(&context, scratch, read);
    if (diag) diag->sha_us += (uint64_t)(esp_timer_get_time() - sha_started_us);
    if (sha_result != 0) {
      result = ESP_FAIL;
      break;
    }
    remaining -= (uint32_t)read;
  }
  if (result == ESP_OK) {
    int64_t started_us = esp_timer_get_time();
    if (mbedtls_sha256_finish(&context, digest) != 0) result = ESP_FAIL;
    if (diag) diag->sha_us += (uint64_t)(esp_timer_get_time() - started_us);
  }
  mbedtls_sha256_free(&context);
  if (!storage_sd_faulted()) fclose(file);
  if (result == ESP_OK) digest_hex(digest, hash);
  if (diag) diag->total_us = (uint64_t)(esp_timer_get_time() - total_started_us);
  return result;
}

static esp_err_t http_write_block(esp_http_client_handle_t client,
                                  const uint8_t *data, size_t bytes,
                                  range_http_diag_t *diag) {
  size_t sent = 0;
  while (sent < bytes) {
    int64_t started_us = esp_timer_get_time();
    int written = esp_http_client_write(client, (const char *)data + sent,
                                        (int)(bytes - sent));
    if (diag) {
      diag->write_us += (uint64_t)(esp_timer_get_time() - started_us);
      diag->write_calls++;
      if (written > 0) diag->bytes_written += (uint32_t)written;
    }
    if (written <= 0) return ESP_ERR_HTTP_WRITE_DATA;
    sent += (size_t)written;
  }
  return ESP_OK;
}

static esp_err_t stream_range_serial(esp_http_client_handle_t client,
                                     const char *path,
                                     uint32_t offset, uint32_t length,
                                     uint8_t *scratch, size_t scratch_size,
                                     range_http_diag_t *diag) {
  if (!storage_sd_mounted()) return ESP_ERR_INVALID_STATE;
  FILE *file = fopen(path, "rb");
  if (!file) {
    int open_errno = errno;
    report_uploader_storage_errno("stream_audio_open", open_errno);
    return open_errno == ENOENT ? ESP_ERR_NOT_FOUND : ESP_FAIL;
  }
  if (fseek(file, 44L + (long)offset, SEEK_SET) != 0) {
    int seek_errno = errno;
    report_uploader_storage_errno("stream_audio_seek", seek_errno);
    if (!storage_sd_faulted()) fclose(file);
    return ESP_FAIL;
  }
  esp_err_t result = ESP_OK;
  uint32_t remaining = length;
  while (result == ESP_OK && remaining) {
    size_t wanted = remaining < scratch_size ? remaining : scratch_size;
    int64_t started_us = esp_timer_get_time();
    size_t read = 0;
    esp_err_t read_result = storage_sd_read(file, scratch, wanted, &read);
    if (diag) {
      diag->sd_read_us += (uint64_t)(esp_timer_get_time() - started_us);
      diag->read_calls++;
      diag->bytes_read += (uint32_t)read;
    }
    if (read_result != ESP_OK || read != wanted) {
      result = ESP_FAIL;
      break;
    }
    result = http_write_block(client, scratch, read, diag);
    remaining -= (uint32_t)read;
  }
  if (!storage_sd_faulted()) fclose(file);
  return result;
}

static esp_err_t cloud_stream_audio_range(const sd_upload_item_t *item,
                                           uint32_t offset, uint32_t length,
                                           uint8_t *scratch,
                                           size_t scratch_size,
                                          const char *sha256,
                                          response_buffer_t *response,
                                          int *http_status,
                                          range_http_diag_t *diag) {
  int64_t total_started_us = esp_timer_get_time();
  if (diag) memset(diag, 0, sizeof(*diag));
  if (!cloud_transport_clock_ready() || !item || !sha256 || !scratch ||
      !length || length > RANGE_BLOCK_BYTES || (offset & 1U) || (length & 1U)) {
    return ESP_ERR_INVALID_ARG;
  }
  char path[SD_UPLOAD_DIR_BYTES + 16];
  snprintf(path, sizeof(path), "%s/audio.wav", item->directory);
  char url[300], key[192];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/audio-range",
           server_base_url(), item->server_session_id);
  snprintf(key, sizeof(key), "range:%s:%lu:%lu:%.*s", item->session_id,
           (unsigned long)offset, (unsigned long)length, 24, sha256);
  esp_http_client_config_t config = {
    .url = url,
    .method = HTTP_METHOD_PUT,
    .timeout_ms = 300000,
    .buffer_size = 2048,
    .buffer_size_tx = HTTP_TX_BUFFER_BYTES,
    .crt_bundle_attach = LUOYE_CFG_ALLOW_INSECURE_HTTP
                           ? NULL : esp_crt_bundle_attach,
  };
  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (!client) return ESP_ERR_NO_MEM;
  if (response) memset(response, 0, sizeof(*response));
  esp_http_client_set_header(client, "Content-Type",
                             "audio/L16;rate=16000;channels=1");
  esp_http_client_set_header(client, "X-Luoye-Protocol",
                             luoye_build_api_contract());
  esp_http_client_set_header(client, "X-Luoye-Firmware", luoye_build_version());
  esp_http_client_set_header(client, "X-Luoye-Device", s_pair.device_id);
  esp_http_client_set_header(client, "Idempotency-Key", key);
  esp_http_client_set_header(client, "X-Content-SHA256", sha256);
  char byte_offset[16], byte_count[16];
  snprintf(byte_offset, sizeof(byte_offset), "%lu", (unsigned long)offset);
  snprintf(byte_count, sizeof(byte_count), "%lu", (unsigned long)length);
  esp_http_client_set_header(client, "X-Byte-Offset", byte_offset);
  esp_http_client_set_header(client, "X-Byte-Count", byte_count);
  set_authorization(client);

  int64_t connect_started_us = esp_timer_get_time();
  esp_err_t error = esp_http_client_open(client, (int)length);
  if (diag) {
    diag->connect_us = (uint64_t)(esp_timer_get_time() - connect_started_us);
  }
  if (error == ESP_OK) {
    error = stream_range_serial(client, path, offset, length, scratch,
                                scratch_size, diag);
  }
  if (error == ESP_OK) {
    int64_t response_started_us = esp_timer_get_time();
    (void)esp_http_client_fetch_headers(client);
    if (response) {
      int read = esp_http_client_read_response(client, response->data,
                                                sizeof(response->data) - 1);
      if (read < 0) {
        error = ESP_ERR_HTTP_FETCH_HEADER;
      } else {
        response->length = (size_t)read;
        response->data[response->length] = '\0';
        if (!esp_http_client_is_complete_data_received(client)) {
          response->overflow = true;
          error = ESP_ERR_INVALID_SIZE;
        }
      }
    }
    if (diag) {
      diag->response_us = (uint64_t)(esp_timer_get_time() - response_started_us);
    }
  }
  int status = esp_http_client_get_status_code(client);
  esp_http_client_cleanup(client);
  if (http_status) *http_status = status;
  if (diag) diag->total_us = (uint64_t)(esp_timer_get_time() - total_started_us);
  return error;
}

static uint32_t request_upload_plan(sd_upload_item_t *item,
                                    range_plan_t *plan) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "range_plan_json");
  cJSON_AddNumberToObject(root, "total_bytes", item->pcm_bytes);
  cJSON_AddNumberToObject(root, "total_samples", item->pcm_bytes / 2U);
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  cJSON_AddStringToObject(root, "mode", item->upload_mode);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "range_plan_json");
  char url[300];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/upload-plan",
           server_base_url(), item->server_session_id);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json",
                                  NULL, body, strlen(body), NULL, NULL,
                                  &response, &status);
  cJSON_free(body);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "range_plan");
  }
  cJSON *reply = cJSON_Parse(response.data);
  uint32_t block = 0, total = 0, covered = 0;
  cJSON *complete = reply ? cJSON_GetObjectItemCaseSensitive(reply, "complete") : NULL;
  cJSON *missing = reply ? cJSON_GetObjectItemCaseSensitive(reply, "missing_ranges") : NULL;
  bool valid = json_u32_value(reply, "block_bytes", &block) &&
               block == RANGE_BLOCK_BYTES &&
               json_u32_value(reply, "total_bytes", &total) &&
               total == item->pcm_bytes &&
               json_u32_value(reply, "covered_bytes", &covered) &&
               covered <= total && cJSON_IsBool(complete) && cJSON_IsArray(missing);
  memset(plan, 0, sizeof(*plan));
  if (valid) {
    plan->complete = cJSON_IsTrue(complete);
    plan->covered_bytes = covered;
    if (!plan->complete) {
      cJSON *first = cJSON_GetArrayItem(missing, 0);
      valid = cJSON_IsObject(first) &&
              json_u32_value(first, "offset", &plan->offset) &&
              json_u32_value(first, "length", &plan->length) &&
              plan->length > 0 && plan->length <= RANGE_BLOCK_BYTES &&
              !(plan->offset & 1U) && !(plan->length & 1U) &&
              plan->offset <= total && plan->length <= total - plan->offset;
    }
  }
  cJSON_Delete(reply);
  if (!valid) {
    return retry_item(item, ESP_ERR_INVALID_RESPONSE, status,
                      "range_plan_ack");
  }
  if (item->acknowledged_bytes != covered ||
      strcmp(item->state, "range_uploading") != 0) {
    item->acknowledged_bytes = covered;
    uint32_t delay = save_stage(item, "range_uploading");
    if (delay) return delay;
  }
  return 0;
}

static uint32_t upload_one_range(sd_upload_item_t *item,
                                 const range_plan_t *plan,
                                 uint8_t *scratch, size_t scratch_size) {
  int64_t range_started_us = esp_timer_get_time();
  wifi_ap_record_t ap = {0};
  esp_err_t ap_error = esp_wifi_sta_get_ap_info(&ap);
  ESP_LOGI(TAG,
           "LY|UPLOAD_DIAG|id=%s event=range_begin route=%s rssi=%d offset=%lu bytes=%lu scratch=%u mode=serial",
           item->session_id, s_use_lan_server ? "lan" : "public",
           ap_error == ESP_OK ? ap.rssi : 0, (unsigned long)plan->offset,
           (unsigned long)plan->length, (unsigned)scratch_size);
  char hash[65];
  range_hash_diag_t hash_diag = {0};
  esp_err_t error = hash_audio_range(item, plan->offset, plan->length,
                                     scratch, scratch_size, hash, &hash_diag);
  ESP_LOGI(TAG,
           "LY|UPLOAD_DIAG|id=%s event=range_hash result=%s source=%s total_ms=%llu sd_read_ms=%llu sha_ms=%llu other_ms=%llu sd_Bps=%llu sha_Bps=%llu read=%lu/%lu reads=%lu",
           item->session_id, esp_err_to_name(error),
           hash_diag.precomputed ? "recording" : "scan",
           diag_ms(hash_diag.total_us),
           diag_ms(hash_diag.sd_read_us), diag_ms(hash_diag.sha_us),
           diag_ms(hash_diag.total_us > hash_diag.sd_read_us + hash_diag.sha_us
                     ? hash_diag.total_us - hash_diag.sd_read_us - hash_diag.sha_us
                     : 0),
           diag_bytes_per_second(hash_diag.bytes_read, hash_diag.sd_read_us),
           diag_bytes_per_second(hash_diag.bytes_read, hash_diag.sha_us),
           (unsigned long)hash_diag.bytes_read, (unsigned long)plan->length,
           (unsigned long)hash_diag.read_calls);
  if (error != ESP_OK) return retry_item(item, error, 0, "range_hash");
  response_buffer_t response = {0};
  int status = 0;
  range_http_diag_t http_diag = {0};
  error = cloud_stream_audio_range(item, plan->offset, plan->length,
                                   scratch, scratch_size, hash,
                                   &response, &status, &http_diag);
  ESP_LOGI(TAG,
           "LY|UPLOAD_DIAG|id=%s event=range_http result=%s http=%d mode=serial total_ms=%llu connect_ms=%llu sd_read_ms=%llu write_ms=%llu response_ms=%llu effective_Bps=%llu write_Bps=%llu read=%lu sent=%lu/%lu reads=%lu writes=%lu",
           item->session_id, esp_err_to_name(error), status,
           diag_ms(http_diag.total_us), diag_ms(http_diag.connect_us),
           diag_ms(http_diag.sd_read_us), diag_ms(http_diag.write_us),
           diag_ms(http_diag.response_us),
           diag_bytes_per_second(http_diag.bytes_written, http_diag.total_us),
           diag_bytes_per_second(http_diag.bytes_written, http_diag.write_us),
           (unsigned long)http_diag.bytes_read,
           (unsigned long)http_diag.bytes_written,
           (unsigned long)plan->length,
           (unsigned long)http_diag.read_calls,
           (unsigned long)http_diag.write_calls);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "audio_range");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *accepted = reply ? cJSON_GetObjectItemCaseSensitive(reply, "accepted") : NULL;
  cJSON *duplicate = reply ? cJSON_GetObjectItemCaseSensitive(reply, "duplicate") : NULL;
  uint32_t offset = 0, length = 0, covered = 0;
  bool valid = (cJSON_IsTrue(accepted) || cJSON_IsTrue(duplicate)) &&
               json_u32_value(reply, "offset", &offset) &&
               offset == plan->offset &&
               json_u32_value(reply, "length", &length) &&
               length == plan->length &&
               json_u32_value(reply, "covered_bytes", &covered) &&
               covered >= plan->covered_bytes && covered <= item->pcm_bytes;
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status,
                                "audio_range_ack");
  item->acknowledged_bytes = covered;
  uint32_t delay = save_stage(item, "range_uploading");
  if (!delay) {
    ESP_LOGI(TAG,
             "LY|UPLOAD|id=%s mode=%s phase=range offset=%lu bytes=%lu covered=%lu/%lu result=acked",
             item->session_id, item->upload_mode, (unsigned long)plan->offset,
             (unsigned long)plan->length, (unsigned long)covered,
             (unsigned long)item->pcm_bytes);
    ESP_LOGI(TAG,
             "LY|UPLOAD_DIAG|id=%s event=range_done result=acked total_ms=%llu covered=%lu/%lu",
             item->session_id,
             diag_ms((uint64_t)(esp_timer_get_time() - range_started_us)),
             (unsigned long)covered, (unsigned long)item->pcm_bytes);
  }
  return delay ? delay : 20;
}

static bool json_u32_value(cJSON *root, const char *name, uint32_t *out) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  if (!cJSON_IsNumber(value) || value->valuedouble < 0 ||
      value->valuedouble > UINT32_MAX) return false;
  uint32_t number = (uint32_t)value->valuedouble;
  if ((double)number != value->valuedouble) return false;
  *out = number;
  return true;
}

static bool live_parse(const sd_upload_item_t *item,
                       const response_buffer_t *response,
                       luoye_live_result_t *out) {
  if (!item || !response || !out || response->overflow) return false;
  cJSON *root = cJSON_Parse(response->data);
  if (!root) return false;
  memset(out, 0, sizeof(*out));
  cJSON *client_id = cJSON_GetObjectItemCaseSensitive(root, "client_session_id");
  cJSON *server_id = cJSON_GetObjectItemCaseSensitive(root, "server_session_id");
  cJSON *scene = cJSON_GetObjectItemCaseSensitive(root, "scene");
  cJSON *status = cJSON_GetObjectItemCaseSensitive(root, "status");
  cJSON *changed = cJSON_GetObjectItemCaseSensitive(root, "changed");
  cJSON *upload = cJSON_GetObjectItemCaseSensitive(root, "upload");
  const char *status_text = cJSON_IsString(status) ? status->valuestring : "";
  bool status_valid = strcmp(status_text, "uploading") == 0 ||
                      strcmp(status_text, "processing") == 0 ||
                      strcmp(status_text, "done") == 0 ||
                      strcmp(status_text, "failed") == 0;
  uint32_t revision = 0, received_samples = 0, contiguous = 0;
  bool valid = cJSON_IsString(client_id) && cJSON_IsString(server_id) &&
      cJSON_IsString(scene) && status_valid && cJSON_IsTrue(changed) &&
      cJSON_IsObject(upload) &&
      strcmp(client_id->valuestring, item->session_id) == 0 &&
      strcmp(server_id->valuestring, item->server_session_id) == 0 &&
      strcmp(scene->valuestring, item->scene) == 0 &&
      json_u32_value(root, "revision", &revision) &&
      json_u32_value(upload, "received_samples", &received_samples) &&
      received_samples <= UINT32_MAX / 2U;
  if (valid) contiguous = received_samples * 2U;
  valid = valid &&
      luoye_live_cursor_accept(item->result_revision,
                               item->result_pcm_bytes,
                               item->acknowledged_bytes,
                               revision, contiguous) &&
      luoye_live_set_text(out->client_session_id,
                          sizeof(out->client_session_id), item->session_id) &&
      luoye_live_set_text(out->server_session_id,
                          sizeof(out->server_session_id), item->server_session_id);
  if (valid && strcmp(item->scene, "meeting") == 0) {
    cJSON *captions = cJSON_GetObjectItemCaseSensitive(root, "captions");
    int count = cJSON_IsArray(captions) ? cJSON_GetArraySize(captions) : 0;
    valid = cJSON_IsArray(captions);
    for (int index = 0; valid && index < count; index++) {
      cJSON *caption = cJSON_GetArrayItem(captions, index);
      cJSON *text = cJSON_IsObject(caption)
                      ? cJSON_GetObjectItemCaseSensitive(caption, "text") : NULL;
      valid = cJSON_IsString(text) &&
              luoye_live_append_text(out->meeting_text,
                                     sizeof(out->meeting_text),
                                     text->valuestring);
    }
    cJSON *speaker = cJSON_GetObjectItemCaseSensitive(root, "speaker");
    if (valid && cJSON_IsObject(speaker)) {
      cJSON *enabled = cJSON_GetObjectItemCaseSensitive(speaker, "enabled");
      uint32_t labeled = 0, speaker_count = 0;
      bool speaker_valid = cJSON_IsBool(enabled) &&
          json_u32_value(speaker, "labeled_segments", &labeled) &&
          labeled <= UINT16_MAX &&
          json_u32_value(speaker, "speaker_count", &speaker_count) &&
          speaker_count <= UINT8_MAX;
      if (speaker_valid) {
        out->speaker_enabled = cJSON_IsTrue(enabled);
        out->speaker_labeled_segments = (uint16_t)labeled;
        out->speaker_count = (uint8_t)speaker_count;
      } else {
        ESP_LOGW(TAG, "LY|SPEAKER|result=invalid_payload");
      }
    }
    out->kind = LUOYE_LIVE_MEETING;
  } else if (valid && strcmp(item->scene, "translate") == 0) {
    cJSON *translations = cJSON_GetObjectItemCaseSensitive(root, "translations");
    int count = cJSON_IsArray(translations) ? cJSON_GetArraySize(translations) : 0;
    valid = cJSON_IsArray(translations);
    for (int index = 0; valid && index < count; index++) {
      cJSON *translation = cJSON_GetArrayItem(translations, index);
      cJSON *source = cJSON_IsObject(translation)
                        ? cJSON_GetObjectItemCaseSensitive(translation,
                                                           "source_text") : NULL;
      cJSON *translated = cJSON_IsObject(translation)
                            ? cJSON_GetObjectItemCaseSensitive(translation,
                                                               "translated_text") : NULL;
      cJSON *source_language = cJSON_IsObject(translation)
                            ? cJSON_GetObjectItemCaseSensitive(translation,
                                                               "source_language") : NULL;
      cJSON *target_language = cJSON_IsObject(translation)
                            ? cJSON_GetObjectItemCaseSensitive(translation,
                                                               "target_language") : NULL;
      valid = cJSON_IsString(source) && cJSON_IsString(translated) &&
              cJSON_IsString(source_language) && cJSON_IsString(target_language) &&
              luoye_live_append_text(out->source_text, sizeof(out->source_text),
                                     source->valuestring) &&
              luoye_live_append_text(out->translated_text,
                                     sizeof(out->translated_text),
                                     translated->valuestring) &&
              luoye_live_set_text(out->source_language,
                                  sizeof(out->source_language),
                                  source_language->valuestring) &&
              luoye_live_set_text(out->target_language,
                                  sizeof(out->target_language),
                                  target_language->valuestring);
    }
    out->kind = LUOYE_LIVE_TRANSLATION;
  } else {
    valid = false;
  }
  if (valid) {
    out->revision = revision;
    out->contiguous_pcm_bytes = contiguous;
    out->final = strcmp(status_text, "done") == 0 ||
                 strcmp(status_text, "failed") == 0;
    out->failed = strcmp(status_text, "failed") == 0;
  }
  cJSON_Delete(root);
  return valid;
}

static void live_publish(const luoye_live_result_t *result) {
  if (!s_live_lock || !result) return;
  xSemaphoreTake(s_live_lock, portMAX_DELAY);
  if (!s_live.client_session_id[0] ||
      strcmp(s_live.client_session_id, result->client_session_id) == 0) {
    luoye_live_result_t merged = *result;
    if (strcmp(s_live.client_session_id, result->client_session_id) == 0 &&
        s_live.kind == result->kind) {
      if (result->kind == LUOYE_LIVE_MEETING) {
        strlcpy(merged.meeting_text, s_live.meeting_text,
                sizeof(merged.meeting_text));
        luoye_live_append_text(merged.meeting_text,
                               sizeof(merged.meeting_text),
                               result->meeting_text);
      } else if (result->kind == LUOYE_LIVE_TRANSLATION) {
        strlcpy(merged.source_text, s_live.source_text,
                sizeof(merged.source_text));
        strlcpy(merged.translated_text, s_live.translated_text,
                sizeof(merged.translated_text));
        luoye_live_append_text(merged.source_text,
                               sizeof(merged.source_text),
                               result->source_text);
        luoye_live_append_text(merged.translated_text,
                               sizeof(merged.translated_text),
                               result->translated_text);
        if (!merged.source_language[0]) {
          strlcpy(merged.source_language, s_live.source_language,
                  sizeof(merged.source_language));
        }
        if (!merged.target_language[0]) {
          strlcpy(merged.target_language, s_live.target_language,
                  sizeof(merged.target_language));
        }
      }
    }
    s_live = merged;
  }
  xSemaphoreGive(s_live_lock);
}

static uint32_t poll_live_result(sd_upload_item_t *item) {
  const char *waiting_state = "uploading";
  char query[48], url[320];
  if (!luoye_live_query(query, sizeof(query), item->result_revision)) {
    return LIVE_POLL_MS;
  }
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/state%s",
           server_base_url(), item->server_session_id, query);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_GET, url, NULL, NULL,
                                  NULL, 0, NULL, NULL,
                                  &response, &status);
  if (error != ESP_OK) {
    return retry_item(item, error, status, "live");
  }
  if (status == 202 || status == 204 || status == 304) {
    if (item->retry_count || strcmp(item->state, waiting_state) != 0) {
      uint32_t delay = save_stage(item, waiting_state);
      if (delay) return delay;
    }
    return LIVE_POLL_MS;
  }
  if (status == 401 || status == 403) {
    return retry_item(item, ESP_OK, status, "live");
  }
  if (luoye_upload_classify_http(true, status) == LUOYE_UPLOAD_HTTP_RETRY) {
    return retry_item(item, ESP_OK, status, "live");
  }
  if (status != 200) {
    ESP_LOGW(TAG, "LY|LIVE|id=%s result=unavailable http=%d",
             item->session_id, status);
    return status == 404 || status == 501 ? 15000 : LIVE_POLL_MS;
  }
  cJSON *state_root = cJSON_Parse(response.data);
  cJSON *changed = state_root
                     ? cJSON_GetObjectItemCaseSensitive(state_root, "changed") : NULL;
  bool unchanged = cJSON_IsFalse(changed);
  cJSON_Delete(state_root);
  if (unchanged) {
    if (item->retry_count || strcmp(item->state, waiting_state) != 0) {
      uint32_t delay = save_stage(item, waiting_state);
      if (delay) return delay;
    }
    return LIVE_POLL_MS;
  }
  luoye_live_result_t result;
  if (!live_parse(item, &response, &result)) {
    ESP_LOGW(TAG, "LY|LIVE|id=%s result=invalid_response http=%d",
             item->session_id, status);
    return LIVE_POLL_MS;
  }
  item->result_revision = result.revision;
  item->result_pcm_bytes = result.contiguous_pcm_bytes;
  item->retry_count = 0;
  item->last_http_status = 0;
  snprintf(item->state, sizeof(item->state), "%s",
           result.failed ? "result_failed" :
           result.final ? "done" : waiting_state);
  if (sd_upload_save(item) != ESP_OK) {
    return retry_item(item, ESP_FAIL, 0, "live_persist");
  }
  live_publish(&result);
  ESP_LOGI(TAG,
           "LY|LIVE|id=%s revision=%lu contiguous=%lu kind=%d final=%d",
           item->session_id, (unsigned long)result.revision,
           (unsigned long)result.contiguous_pcm_bytes,
           (int)result.kind, result.final);
  if (result.kind == LUOYE_LIVE_MEETING) {
    ESP_LOGI(TAG,
             "LY|SPEAKER|id=%s enabled=%d labeled=%u speakers=%u",
             item->session_id, result.speaker_enabled,
             (unsigned)result.speaker_labeled_segments,
             (unsigned)result.speaker_count);
  }
  return result.final ? 3000 : LIVE_POLL_MS;
}

static uint32_t upload_one_mark(sd_upload_item_t *item, const char *line_text,
                                uint32_t index) {
  cJSON *line = cJSON_Parse(line_text);
  cJSON *kind = line ? cJSON_GetObjectItemCaseSensitive(line, "kind") : NULL;
  cJSON *at = line ? cJSON_GetObjectItemCaseSensitive(line, "at_ms") : NULL;
  bool valid = cJSON_IsString(kind) && kind->valuestring[0] &&
      cJSON_IsNumber(at) && at->valuedouble >= 0 &&
      at->valuedouble <= (double)UINT32_MAX &&
      (double)(uint32_t)at->valuedouble == at->valuedouble;
  if (!valid) {
    cJSON_Delete(line);
    ESP_LOGW(TAG, "LY|UPLOAD|id=%s phase=marks result=invalid_line line=%lu",
             item->session_id, (unsigned long)index);
    return 0;
  }
  uint64_t sample64 = (uint64_t)(uint32_t)at->valuedouble * 16U;
  uint32_t total_samples = item->pcm_bytes / 2U;
  uint32_t offset_samples = sample64 > total_samples
                              ? total_samples : (uint32_t)sample64;
  char mark_id[24], key[192], url[320];
  snprintf(mark_id, sizeof(mark_id), "mark-%06lu", (unsigned long)index);
  if (!luoye_upload_mark_key(key, sizeof(key), item->session_id, mark_id)) {
    cJSON_Delete(line);
    return retry_item(item, ESP_ERR_INVALID_SIZE, 0, "mark_key");
  }
  cJSON *request = cJSON_CreateObject();
  if (!request) {
    cJSON_Delete(line);
    return retry_item(item, ESP_ERR_NO_MEM, 0, "mark_json");
  }
  cJSON_AddNumberToObject(request, "offset_samples", offset_samples);
  cJSON_AddStringToObject(request, "kind", kind->valuestring);
  char *body = cJSON_PrintUnformatted(request);
  cJSON_Delete(request);
  cJSON_Delete(line);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "mark_json");
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/marks/%s",
           server_base_url(), item->server_session_id, mark_id);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_PUT, url, "application/json", key,
                                  body, strlen(body), NULL, NULL,
                                  &response, &status);
  cJSON_free(body);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "mark");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *accepted = reply
                      ? cJSON_GetObjectItemCaseSensitive(reply, "accepted") : NULL;
  cJSON *duplicate = reply
                       ? cJSON_GetObjectItemCaseSensitive(reply, "duplicate") : NULL;
  uint32_t revision = 0;
  valid = (cJSON_IsTrue(accepted) || cJSON_IsTrue(duplicate)) &&
          json_u32_value(reply, "revision", &revision) && revision > 0;
  cJSON_Delete(reply);
  if (!valid) {
    return retry_item(item, ESP_ERR_INVALID_RESPONSE, status, "mark_ack");
  }
  return 0;
}

static uint32_t upload_marks(sd_upload_item_t *item, uint8_t *marks_buffer,
                             bool close_time) {
  if (!storage_sd_mounted()) {
    return retry_item(item, ESP_ERR_INVALID_STATE, 0, "read_marks_gate");
  }
  char path[SD_UPLOAD_DIR_BYTES + 24];
  snprintf(path, sizeof(path), "%s/marks.jsonl", item->directory);
  FILE *stream = fopen(path, "rb");
  if (!stream) {
    int open_errno = errno;
    report_uploader_storage_errno("marks_open", open_errno);
    return retry_item(item,
                      open_errno == ENOENT ? ESP_ERR_NOT_FOUND : ESP_FAIL,
                      0, "read_marks");
  }

  uint32_t index = 0;
  for (;;) {
    if (storage_sd_faulted()) {
      return retry_item(item, ESP_FAIL, 0, "read_marks_loop_gate");
    }
    size_t line_length = 0;
    luoye_upload_mark_read_t read = luoye_upload_read_mark_line(
        stream, (char *)marks_buffer, MARKS_BUFFER_BYTES,
        &index, &line_length);
    if (read == LUOYE_UPLOAD_MARK_EOF) break;
    if (read == LUOYE_UPLOAD_MARK_IO_ERROR) {
      int read_errno = errno;
      report_uploader_storage_errno("marks_read", read_errno);
      if (!storage_sd_faulted()) fclose(stream);
      return retry_item(item, ESP_FAIL, 0, "read_marks");
    }
    if (read == LUOYE_UPLOAD_MARK_SKIPPED) {
      ESP_LOGW(TAG,
               "LY|UPLOAD|id=%s phase=marks result=skipped_line line=%lu max=%u",
               item->session_id, (unsigned long)index,
               (unsigned)(MARKS_BUFFER_BYTES - 1U));
      continue;
    }
    (void)line_length;
    uint32_t delay = upload_one_mark(item, (char *)marks_buffer, index);
    if (delay) {
      if (!storage_sd_faulted()) fclose(stream);
      return delay;
    }
  }
  if (!storage_sd_faulted()) fclose(stream);
  if (storage_sd_faulted()) {
    return retry_item(item, ESP_FAIL, 0, "read_marks_close_gate");
  }
  if (!close_time) {
    ESP_LOGI(TAG, "LY|UPLOAD|id=%s phase=marks result=live_acked",
             item->session_id);
    return 20;
  }
  item->marks_acked = true;
  uint32_t delay = save_stage(item, "awaiting_final");
  if (!delay) ESP_LOGI(TAG, "LY|UPLOAD|id=%s phase=marks result=acked", item->session_id);
  return delay ? delay : 20;
}

/* MARK is a live timeline event, not merely a close-time sidecar.  Track the
 * durable JSONL size so newly fsynced marks are pushed while recording.  A
 * reboot may replay existing lines once; mark PUTs are idempotent. */
static char s_live_marks_session[SD_UPLOAD_SESSION_ID_BYTES];
static off_t s_live_marks_uploaded_size = -1;

static bool live_marks_changed(const sd_upload_item_t *item, off_t *size_out) {
  char path[SD_UPLOAD_DIR_BYTES + 24];
  struct stat st = {0};
  if (strcmp(s_live_marks_session, item->session_id) != 0) {
    strlcpy(s_live_marks_session, item->session_id,
            sizeof(s_live_marks_session));
    s_live_marks_uploaded_size = -1;
  }
  snprintf(path, sizeof(path), "%s/marks.jsonl", item->directory);
  if (!storage_sd_mounted()) return false;
  if (stat(path, &st) != 0) {
    report_uploader_storage_errno("marks_stat", errno);
    return false;
  }
  if (st.st_size == s_live_marks_uploaded_size) return false;
  if (size_out) *size_out = st.st_size;
  return true;
}

static uint32_t delete_cloud_accepted(sd_upload_item_t *item) {
  uint64_t freed = 0;
  esp_err_t error = sd_storage_delete_local(item->binding_generation,
                                            item->session_id, &freed);
  if (error == ESP_OK || error == ESP_ERR_NOT_FOUND) {
    ESP_LOGI(TAG,
             "LY|UPLOAD|id=%s phase=local_delete result=ok freed=%llu",
             item->session_id, (unsigned long long)freed);
    return 20;
  }
  ESP_LOGW(TAG,
           "LY|UPLOAD|id=%s phase=local_delete result=retry err=%s",
           item->session_id, esp_err_to_name(error));
  return 3000;
}

static uint32_t defer_gap_session(sd_upload_item_t *item) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "defer_json");
  cJSON_AddNumberToObject(root, "total_bytes", item->pcm_bytes);
  cJSON_AddNumberToObject(root, "total_samples", item->pcm_bytes / 2U);
  if (!json_add_utc_or_null(root, "ended_at_utc", item->ended_at_utc)) {
    cJSON_Delete(root);
    return retry_item(item, ESP_ERR_INVALID_ARG, 0, "defer_time");
  }
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "defer_json");
  char url[300], key[192];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/defer",
           server_base_url(), item->server_session_id);
  snprintf(key, sizeof(key), "defer:%s:%lu", item->session_id,
           (unsigned long)item->pcm_bytes);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json",
                                  key, body, strlen(body), NULL, NULL,
                                  &response, &status);
  cJSON_free(body);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "defer");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *state = reply ? cJSON_GetObjectItemCaseSensitive(reply, "status") : NULL;
  uint32_t missing = 0;
  bool valid = cJSON_IsString(state) &&
      strcmp(state->valuestring, "awaiting_repair") == 0 &&
      json_u32_value(reply, "missing_bytes", &missing);
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status,
                                "defer_ack");
  item->defer_acked = true;
  item->live_resume_required = false;
  uint32_t delay = save_stage(item, "manual_sync_pending");
  if (!delay) {
    ESP_LOGI(TAG,
             "LY|LIVE_GAP|id=%s state=deferred missing_bytes=%lu manual_sync=required",
             item->session_id, (unsigned long)missing);
  }
  return delay ? delay : 20;
}

static uint32_t finalize_session(sd_upload_item_t *item) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "final_json");
  uint32_t total_chunks = item->pcm_bytes / CHUNK_BYTES;
  if (item->pcm_bytes % CHUNK_BYTES) total_chunks++;
  cJSON_AddNumberToObject(root, "total_chunks", total_chunks);
  cJSON_AddNumberToObject(root, "total_samples", item->pcm_bytes / 2U);
  if (!json_add_utc_or_null(root, "ended_at_utc", item->ended_at_utc)) {
    cJSON_Delete(root);
    return retry_item(item, ESP_ERR_INVALID_ARG, 0, "final_time");
  }
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "final_json");
  char key[192], url[280];
  if (!luoye_upload_final_key(key, sizeof(key), item->session_id)) {
    cJSON_free(body);
    return retry_item(item, ESP_ERR_INVALID_SIZE, 0, "final_key");
  }
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/end",
           server_base_url(), item->server_session_id);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json", key,
      body, strlen(body), NULL, NULL, &response, &status);
  cJSON_free(body);
  luoye_upload_http_class_t classification =
      luoye_upload_classify_http(error == ESP_OK, status);
  if (classification == LUOYE_UPLOAD_HTTP_CONFLICT) {
    cJSON *conflict = cJSON_Parse(response.data);
    cJSON *error_json = conflict
                          ? cJSON_GetObjectItemCaseSensitive(conflict, "error") : NULL;
    cJSON *code = cJSON_IsObject(error_json)
                    ? cJSON_GetObjectItemCaseSensitive(error_json, "code") : NULL;
    cJSON *missing = conflict
                       ? cJSON_GetObjectItemCaseSensitive(conflict,
                                                          "missing_sequences") : NULL;
    cJSON *first = cJSON_IsArray(missing) ? cJSON_GetArrayItem(missing, 0) : NULL;
    bool recoverable = cJSON_IsString(code) &&
        strcmp(code->valuestring, "AUDIO_CHUNKS_MISSING") == 0 &&
        cJSON_IsNumber(first) && first->valuedouble >= 0 &&
        first->valuedouble < total_chunks &&
        (double)(uint32_t)first->valuedouble == first->valuedouble;
    if (recoverable) {
      item->next_seq = (uint32_t)first->valuedouble;
      uint64_t offset = (uint64_t)item->next_seq * CHUNK_BYTES;
      item->acknowledged_bytes = offset > item->pcm_bytes
                                   ? item->pcm_bytes : (uint32_t)offset;
      cJSON_Delete(conflict);
      uint32_t delay = save_stage(item, "uploading");
      ESP_LOGW(TAG,
               "LY|UPLOAD|id=%s phase=final result=missing rewind_seq=%lu",
               item->session_id, (unsigned long)item->next_seq);
      return delay ? delay : 20;
    }
    cJSON_Delete(conflict);
  }
  if (classification != LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "final");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *state = reply ? cJSON_GetObjectItemCaseSensitive(reply, "status") : NULL;
  cJSON *missing = reply
                     ? cJSON_GetObjectItemCaseSensitive(reply,
                                                        "missing_sequences") : NULL;
  bool valid = cJSON_IsString(state) &&
      (strcmp(state->valuestring, "processing") == 0 ||
       strcmp(state->valuestring, "done") == 0 ||
       strcmp(state->valuestring, "failed") == 0) &&
      cJSON_IsArray(missing) && cJSON_GetArraySize(missing) == 0;
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status, "final_ack");
  item->final_acked = true;
  uint32_t delay = save_stage(item, "delete_pending");
  if (!delay) {
    ESP_LOGI(TAG,
             "LY|UPLOAD|id=%s phase=final result=upload_complete local_delete=pending",
             item->session_id);
  }
  return delay ? delay : delete_cloud_accepted(item);
}

static uint32_t complete_range_session(sd_upload_item_t *item) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return retry_item(item, ESP_ERR_NO_MEM, 0, "range_complete_json");
  cJSON_AddNumberToObject(root, "total_bytes", item->pcm_bytes);
  cJSON_AddNumberToObject(root, "total_samples", item->pcm_bytes / 2U);
  if (!json_add_utc_or_null(root, "ended_at_utc", item->ended_at_utc)) {
    cJSON_Delete(root);
    return retry_item(item, ESP_ERR_INVALID_ARG, 0, "range_complete_time");
  }
  cJSON_AddNumberToObject(root, "binding_generation", item->binding_generation);
  char *body = cJSON_PrintUnformatted(root);
  cJSON_Delete(root);
  if (!body) return retry_item(item, ESP_ERR_NO_MEM, 0, "range_complete_json");
  char url[300], key[192];
  snprintf(url, sizeof(url), "%s/api/v2/device/sessions/%s/complete",
           server_base_url(), item->server_session_id);
  snprintf(key, sizeof(key), "complete:%s:%lu", item->session_id,
           (unsigned long)item->pcm_bytes);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json",
                                  key, body, strlen(body), NULL, NULL,
                                  &response, &status);
  cJSON_free(body);
  if (luoye_upload_classify_http(error == ESP_OK, status) !=
      LUOYE_UPLOAD_HTTP_OK) {
    return retry_item(item, error, status, "range_complete");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *state = reply ? cJSON_GetObjectItemCaseSensitive(reply, "status") : NULL;
  cJSON *complete = reply ? cJSON_GetObjectItemCaseSensitive(reply, "complete") : NULL;
  cJSON *missing = reply ? cJSON_GetObjectItemCaseSensitive(reply, "missing_ranges") : NULL;
  bool valid = cJSON_IsString(state) &&
      (strcmp(state->valuestring, "processing") == 0 ||
       strcmp(state->valuestring, "done") == 0 ||
       strcmp(state->valuestring, "failed") == 0) &&
      cJSON_IsTrue(complete) && cJSON_IsArray(missing) &&
      cJSON_GetArraySize(missing) == 0;
  cJSON_Delete(reply);
  if (!valid) return retry_item(item, ESP_ERR_INVALID_RESPONSE, status,
                                "range_complete_ack");
  item->acknowledged_bytes = item->pcm_bytes;
  item->final_acked = true;
  uint32_t delay = save_stage(item, "delete_pending");
  if (!delay) {
    ESP_LOGI(TAG,
             "LY|UPLOAD|id=%s mode=%s phase=complete result=cloud_accepted local_delete=pending",
             item->session_id, item->upload_mode);
  }
  return delay ? delay : delete_cloud_accepted(item);
}

static uint32_t process_upload_item(sd_upload_item_t *item, uint8_t *audio_buffer,
                                    uint8_t *marks_buffer,
                                    uint8_t *range_buffer) {
  if (item->final_acked) return delete_cloud_accepted(item);
  if (item->live_resume_required && !item->local_closed) {
    if (!item->remote_session_created) return create_remote_session(item);
    return resume_live_epoch(item);
  }
  if (item->local_closed && strcmp(item->upload_mode, "live") == 0) {
    uint32_t lag = item->pcm_bytes - item->acknowledged_bytes;
    if (item->live_resume_required && lag > 0) {
      item->deferred_gaps = true;
      item->live_resume_required = false;
      uint32_t delay = save_stage(item, "manual_sync_pending");
      if (delay) return delay;
    }
    if (item->deferred_gaps && item->remote_session_created) {
      if (!item->marks_acked) return upload_marks(item, marks_buffer, true);
      if (!item->defer_acked) return defer_gap_session(item);
      if (!s_manual_sync) return 1000;
      snprintf(item->upload_mode, sizeof(item->upload_mode), "repair");
      uint32_t delay = save_stage(item, "repair_pending");
      if (delay) return delay;
    } else if (!item->remote_session_created || item->acknowledged_bytes == 0) {
      if (!s_manual_sync && item->deferred_gaps) return 1000;
      snprintf(item->upload_mode, sizeof(item->upload_mode), "bulk");
      uint32_t delay = save_stage(item, "bulk_pending");
      if (delay) return delay;
    } else if (lag >= CHUNK_BYTES) {
      snprintf(item->upload_mode, sizeof(item->upload_mode), "repair");
      uint32_t delay = save_stage(item, "repair_pending");
      if (delay) return delay;
    }
  }
  if (!item->remote_session_created) return create_remote_session(item);
  bool range_mode = strcmp(item->upload_mode, "bulk") == 0 ||
                    strcmp(item->upload_mode, "repair") == 0;
  if (range_mode) {
    range_plan_t plan;
    uint32_t delay = request_upload_plan(item, &plan);
    if (delay) return delay;
    if (!plan.complete) {
      return upload_one_range(item, &plan, range_buffer, RANGE_STREAM_BYTES);
    }
    item->acknowledged_bytes = item->pcm_bytes;
    if (!item->marks_acked) return upload_marks(item, marks_buffer, true);
    return complete_range_session(item);
  }
  int64_t now_ms = esp_timer_get_time() / 1000;
  if (!item->local_closed && now_ms >= s_next_live_poll_ms) {
    s_next_live_poll_ms = now_ms + LIVE_POLL_DEADLINE_MS;
    uint32_t delay = poll_live_result(item);
    return delay > LIVE_POLL_DEADLINE_MS ? LIVE_POLL_DEADLINE_MS : delay;
  }
  off_t marks_size = 0;
  if (!item->local_closed && live_marks_changed(item, &marks_size)) {
    uint32_t delay = upload_marks(item, marks_buffer, false);
    if (delay <= 20) s_live_marks_uploaded_size = marks_size;
    return delay;
  }
  if (item->acknowledged_bytes < item->pcm_bytes) {
    uint32_t delay = upload_one_chunk(item, audio_buffer);
    if (delay) return delay;
  }
  if (!item->local_closed) return poll_live_result(item);
  if (!item->marks_acked) return upload_marks(item, marks_buffer, true);
  if (!item->final_acked) return finalize_session(item);
  return delete_cloud_accepted(item);
}

static uint32_t todo_retry(luoye_todo_item_t *item, esp_err_t error,
                           int http_status, const char *phase) {
  luoye_upload_http_class_t classification =
      luoye_upload_classify_http(error == ESP_OK, http_status);
  item->last_http_status = http_status;
  if (classification == LUOYE_UPLOAD_HTTP_AUTH) {
    /* Token expiry is not a todo failure.  Preserve the exact durable stage;
       same-generation re-claim resumes it, while a different generation is
       naturally filtered by todo_next(). */
    todo_save(item);
    auth_repair_binding(http_status);
    ESP_LOGW(TAG, "LY|TODO|id=%s phase=%s result=auth_blocked http=%d",
             item->id, phase, http_status);
    return 30000;
  }
  if (classification == LUOYE_UPLOAD_HTTP_CONFLICT) {
    item->state = LUOYE_TODO_FAILED;
    todo_save(item);
    if (s_post) s_post(APP_EV_TODO_RESULT, -1);
    ESP_LOGE(TAG, "LY|TODO|id=%s phase=%s result=conflict http=%d",
             item->id, phase, http_status);
    return 30000;
  }
  if (classification == LUOYE_UPLOAD_HTTP_PERMANENT) {
    item->state = LUOYE_TODO_FAILED;
    todo_save(item);
    if (s_post) s_post(APP_EV_TODO_RESULT, -1);
    ESP_LOGE(TAG, "LY|TODO|id=%s phase=%s result=permanent http=%d",
             item->id, phase, http_status);
    return 30000;
  }
  item->retry_count++;
  todo_save(item);
  uint32_t delay = luoye_upload_retry_delay_ms(item->retry_count, esp_random());
  ESP_LOGW(TAG,
           "LY|TODO|id=%s phase=%s result=retry attempt=%lu err=%s http=%d delay_ms=%lu",
           item->id, phase, (unsigned long)item->retry_count,
           esp_err_to_name(error), http_status, (unsigned long)delay);
  return delay;
}

static uint32_t agenda_sync_once(bool notify_ui) {
  luoye_agenda_snapshot_t snapshot;
  uint32_t revision = agenda_snapshot_get(&snapshot) &&
                      snapshot.binding_generation == s_binding_generation
                        ? snapshot.revision : 0;
  char query[48], url[320];
  if (!luoye_agenda_query(query, sizeof(query), revision)) return AGENDA_POLL_MS;
  snprintf(url, sizeof(url), "%s/api/v2/device/agenda%s&window_days=7",
           server_base_url(), query);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_GET, url, NULL, NULL, NULL, 0,
                                  NULL, NULL, &response, &status);
  if (error != ESP_OK) {
    ESP_LOGW(TAG, "LY|AGENDA|event=sync result=transport err=%s",
             esp_err_to_name(error));
    return AGENDA_POLL_MS;
  }
  if (status == 202 || status == 204 || status == 304) return AGENDA_POLL_MS;
  if (status == 401 || status == 403) {
    auth_repair_binding(status);
    return 30000;
  }
  if (status != 200) {
    ESP_LOGW(TAG, "LY|AGENDA|event=sync result=unavailable http=%d", status);
    return AGENDA_POLL_MS;
  }
  cJSON *envelope = cJSON_Parse(response.data);
  uint32_t server_time_utc = 0, response_binding = 0;
  bool server_time_valid = envelope &&
      json_u32_value(envelope, "server_time_utc", &server_time_utc) &&
      json_u32_value(envelope, "binding_generation", &response_binding) &&
      response_binding == s_binding_generation;
  cJSON_Delete(envelope);
  error = agenda_apply_server_json(response.data, s_binding_generation);
  if ((error == ESP_OK || error == ESP_ERR_INVALID_STATE) &&
      server_time_valid) {
    /* INVALID_STATE means a fully valid snapshot with an already-cached
       revision.  Its current-account server clock is still trustworthy. */
    bootstrap_clock_from_server((int64_t)server_time_utc, "agenda");
  }
  if (error == ESP_OK) {
    luoye_agenda_snapshot_t updated;
    agenda_snapshot_get(&updated);
    ESP_LOGI(TAG, "LY|AGENDA|event=sync revision=%lu items=%u",
             (unsigned long)updated.revision, updated.count);
    if (s_idle_agenda_maintenance) s_idle_agenda_changed = true;
    if (s_post && (notify_ui || !s_idle_agenda_maintenance)) {
      s_post(APP_EV_AGENDA_CHANGE, (int32_t)updated.revision);
    }
  } else if (error != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "LY|AGENDA|event=sync result=invalid_response esp=%s",
             esp_err_to_name(error));
  }
  return AGENDA_POLL_MS;
}

static uint32_t todo_upload_audio(luoye_todo_item_t *item, uint8_t *buffer,
                                  size_t capacity) {
  size_t size = 0;
  esp_err_t error = todo_read_audio(item, buffer, capacity, &size);
  if (error != ESP_OK) return todo_retry(item, error, 0, "read_audio");
  char url[384], key[112], hash[65];
  snprintf(url, sizeof(url),
           "%s/api/v2/device/todos/%s/audio?binding_generation=%lu",
           server_base_url(), item->id,
           (unsigned long)item->binding_generation);
  snprintf(key, sizeof(key), "todo:%s:audio", item->id);
  sha256_hex(buffer, size, hash);
  response_buffer_t response = {0};
  int status = 0;
  error = cloud_request(HTTP_METHOD_PUT, url, "audio/wav", key, buffer, size,
                        NULL, hash, &response, &status);
  if (error != ESP_OK || status != 200) {
    return todo_retry(item, error, status, "audio");
  }
  cJSON *root = cJSON_Parse(response.data);
  cJSON *accepted = root
                      ? cJSON_GetObjectItemCaseSensitive(root, "accepted") : NULL;
  cJSON *duplicate = root
                       ? cJSON_GetObjectItemCaseSensitive(root, "duplicate") : NULL;
  cJSON *client_id = root
                       ? cJSON_GetObjectItemCaseSensitive(root,
                                                          "client_todo_id") : NULL;
  cJSON *server_id = root
                       ? cJSON_GetObjectItemCaseSensitive(root, "server_id") : NULL;
  cJSON *remote_state = root
                          ? cJSON_GetObjectItemCaseSensitive(root, "status") : NULL;
  uint32_t revision = 0;
  bool valid = (cJSON_IsTrue(accepted) || cJSON_IsTrue(duplicate)) &&
      cJSON_IsString(client_id) && strcmp(client_id->valuestring, item->id) == 0 &&
      cJSON_IsString(server_id) && server_id->valuestring[0] &&
      strlen(server_id->valuestring) < sizeof(item->server_id) &&
      luoye_upload_safe_path_id(server_id->valuestring) &&
      cJSON_IsString(remote_state) &&
      (strcmp(remote_state->valuestring, "received") == 0 ||
       strcmp(remote_state->valuestring, "processing") == 0 ||
       strcmp(remote_state->valuestring, "ready") == 0 ||
       strcmp(remote_state->valuestring, "confirmed") == 0 ||
       strcmp(remote_state->valuestring, "cancelled") == 0 ||
       strcmp(remote_state->valuestring, "failed") == 0) &&
      json_u32_value(root, "revision", &revision) && revision > 0;
  bool remote_failed = valid &&
      strcmp(remote_state->valuestring, "failed") == 0;
  if (valid) {
    snprintf(item->server_id, sizeof(item->server_id), "%s",
             server_id->valuestring);
  }
  cJSON_Delete(root);
  if (!valid) return todo_retry(item, ESP_ERR_INVALID_RESPONSE, status, "audio_ack");
  item->state = remote_failed ? LUOYE_TODO_FAILED : LUOYE_TODO_UPLOADED;
  item->retry_count = 0;
  item->last_http_status = 0;
  if (todo_save(item) != ESP_OK) return todo_retry(item, ESP_FAIL, 0, "persist");
  ESP_LOGI(TAG, "LY|TODO|id=%s phase=audio result=acked bytes=%u",
           item->id, (unsigned)size);
  if (remote_failed && s_post) s_post(APP_EV_TODO_RESULT, -1);
  return 100;
}

static bool todo_json_i64(cJSON *root, const char *name, int64_t *out) {
  cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
  if (!cJSON_IsNumber(value) || value->valuedouble <= 0 ||
      value->valuedouble > 9007199254740991.0) return false;
  int64_t converted = (int64_t)value->valuedouble;
  if ((double)converted != value->valuedouble) return false;
  *out = converted;
  return true;
}

static uint32_t todo_poll_result(luoye_todo_item_t *item) {
  char url[384];
  snprintf(url, sizeof(url),
           "%s/api/v2/device/todos/%s/result?after_revision=%lu",
           server_base_url(),
           item->server_id[0] ? item->server_id : item->id,
           (unsigned long)item->result_revision);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_GET, url, NULL, NULL, NULL, 0,
                                  NULL, NULL, &response, &status);
  if (error != ESP_OK) return todo_retry(item, error, status, "result");
  if (status == 202 || status == 204 || status == 304) return 3000;
  if (status != 200) return todo_retry(item, ESP_OK, status, "result");
  cJSON *root = cJSON_Parse(response.data);
  cJSON *todo_id = root ? cJSON_GetObjectItemCaseSensitive(root, "todo_id") : NULL;
  cJSON *server_id = root ? cJSON_GetObjectItemCaseSensitive(root, "server_id") : NULL;
  cJSON *state = root ? cJSON_GetObjectItemCaseSensitive(root, "status") : NULL;
  cJSON *transcript = root ? cJSON_GetObjectItemCaseSensitive(root, "transcript") : NULL;
  cJSON *title = root ? cJSON_GetObjectItemCaseSensitive(root, "title") : NULL;
  cJSON *display = root ? cJSON_GetObjectItemCaseSensitive(root, "display_time") : NULL;
  cJSON *generation = root ? cJSON_GetObjectItemCaseSensitive(root, "binding_generation") : NULL;
  cJSON *revision_json = root ? cJSON_GetObjectItemCaseSensitive(root, "revision") : NULL;
  bool valid = cJSON_IsString(todo_id) && cJSON_IsString(server_id) &&
      cJSON_IsString(state) &&
      cJSON_IsNumber(generation) && cJSON_IsNumber(revision_json) &&
      strcmp(todo_id->valuestring, item->id) == 0 &&
      item->server_id[0] &&
      strcmp(server_id->valuestring, item->server_id) == 0 &&
      luoye_upload_safe_path_id(server_id->valuestring) &&
      generation->valuedouble == item->binding_generation &&
      revision_json->valuedouble > item->result_revision &&
      revision_json->valuedouble <= UINT32_MAX;
  uint32_t revision = valid ? (uint32_t)revision_json->valuedouble : 0;
  if (valid && (double)revision != revision_json->valuedouble) valid = false;
  const char *state_text = valid ? state->valuestring : "";
  bool needs_confirmation = strcmp(state_text, "needs_confirmation") == 0;
  bool created = strcmp(state_text, "created") == 0;
  bool failed = strcmp(state_text, "failed") == 0;
  if (!valid || (!needs_confirmation && !created && !failed)) {
    cJSON_Delete(root);
    return todo_retry(item, ESP_ERR_INVALID_RESPONSE, status, "result_json");
  }
  if (failed) {
    item->state = LUOYE_TODO_FAILED;
    todo_save(item);
    cJSON_Delete(root);
    if (s_post) s_post(APP_EV_TODO_RESULT, -1);
    return 30000;
  }
  int64_t due = 0;
  cJSON *due_json = cJSON_GetObjectItemCaseSensitive(root, "due_at_utc");
  bool due_valid = cJSON_IsNull(due_json) ||
                   todo_json_i64(root, "due_at_utc", &due);
  if (!cJSON_IsString(transcript) || !cJSON_IsString(title) ||
      !cJSON_IsString(display) || !due_valid) {
    cJSON_Delete(root);
    return todo_retry(item, ESP_ERR_INVALID_RESPONSE, status, "result_content");
  }
  error = todo_set_result(item->id, server_id->valuestring, revision,
                          transcript->valuestring, title->valuestring, due,
                          display->valuestring, needs_confirmation);
  cJSON_Delete(root);
  if (error != ESP_OK) return todo_retry(item, error, status, "result_persist");
  ESP_LOGI(TAG, "LY|TODO|id=%s phase=result revision=%lu status=%s",
           item->id, (unsigned long)revision,
           needs_confirmation ? "needs_confirmation" : "created");
  if (s_post) s_post(APP_EV_TODO_RESULT, needs_confirmation ? 1 : 2);
  return 3000;
}

static uint32_t todo_send_action(luoye_todo_item_t *item) {
  bool confirm = item->state == LUOYE_TODO_CONFIRM_PENDING;
  char url[384], key[112], body[96];
  snprintf(url, sizeof(url), "%s/api/v2/device/todos/%s/actions",
           server_base_url(),
           item->server_id[0] ? item->server_id : item->id);
  snprintf(key, sizeof(key), "todo:%s:action:%s:%lu", item->id,
           confirm ? "confirm" : "cancel", (unsigned long)item->result_revision);
  snprintf(body, sizeof(body), "{\"action\":\"%s\",\"revision\":%lu}",
           confirm ? "confirm" : "cancel", (unsigned long)item->result_revision);
  response_buffer_t response = {0};
  int status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json", key,
                                  body, strlen(body), NULL, NULL,
                                  &response, &status);
  if (error == ESP_OK && status == 409) {
    cJSON *root = cJSON_Parse(response.data);
    cJSON *error_json = root
                          ? cJSON_GetObjectItemCaseSensitive(root, "error") : NULL;
    cJSON *code = cJSON_IsObject(error_json)
                    ? cJSON_GetObjectItemCaseSensitive(error_json, "code") : NULL;
    uint32_t current_revision = 0;
    bool revision_mismatch = cJSON_IsString(code) &&
        strcmp(code->valuestring, "TODO_REVISION_MISMATCH") == 0 &&
        json_u32_value(root, "current_revision", &current_revision) &&
        current_revision > item->result_revision;
    cJSON_Delete(root);
    if (revision_mismatch) {
      item->state = LUOYE_TODO_UPLOADED;
      item->retry_count = 0;
      item->last_http_status = status;
      if (todo_save(item) != ESP_OK) {
        return todo_retry(item, ESP_FAIL, 0, "action_repoll_persist");
      }
      ESP_LOGW(TAG,
               "LY|TODO|id=%s phase=action result=revision_mismatch local=%lu server=%lu",
               item->id, (unsigned long)item->result_revision,
               (unsigned long)current_revision);
      return 100;
    }
  }
  if (error != ESP_OK || status != 200) {
    return todo_retry(item, error, status, "action");
  }
  cJSON *reply = cJSON_Parse(response.data);
  cJSON *reply_id = reply
                      ? cJSON_GetObjectItemCaseSensitive(reply,
                                                         "client_todo_id") : NULL;
  cJSON *reply_state = reply
                         ? cJSON_GetObjectItemCaseSensitive(reply, "status") : NULL;
  uint32_t reply_revision = 0;
  bool ack_valid = cJSON_IsString(reply_id) &&
      strcmp(reply_id->valuestring, item->id) == 0 &&
      cJSON_IsString(reply_state) &&
      strcmp(reply_state->valuestring,
             confirm ? "confirmed" : "cancelled") == 0 &&
      json_u32_value(reply, "revision", &reply_revision) &&
      reply_revision == item->result_revision + 1U;
  cJSON_Delete(reply);
  if (!ack_valid) {
    return todo_retry(item, ESP_ERR_INVALID_RESPONSE, status, "action_ack");
  }
  luoye_todo_state_t pending_state = item->state;
  item->state = confirm ? LUOYE_TODO_CREATED : LUOYE_TODO_CANCELLED;
  item->retry_count = 0;
  item->last_http_status = 0;
  if (todo_save(item) != ESP_OK) {
    item->state = pending_state;
    return todo_retry(item, ESP_FAIL, 0, "action_persist");
  }
  ESP_LOGI(TAG, "LY|TODO|id=%s phase=action result=%s",
           item->id, confirm ? "created" : "cancelled");
  if (confirm && s_post) s_post(APP_EV_TODO_RESULT, 2);
  return 100;
}

static uint32_t process_todo(luoye_todo_item_t *item, uint8_t *audio_buffer,
                             size_t capacity) {
  if (item->state == LUOYE_TODO_QUEUED) {
    return todo_upload_audio(item, audio_buffer, capacity);
  }
  if (item->state == LUOYE_TODO_UPLOADED) return todo_poll_result(item);
  if (item->state == LUOYE_TODO_CONFIRM_PENDING ||
      item->state == LUOYE_TODO_CANCEL_PENDING) return todo_send_action(item);
  return 3000;
}

static void sntp_sync_cb(struct timeval *tv) {
  (void)tv;
  if (s_time_task) xTaskNotifyGive(s_time_task);
}

static void time_sync_task(void *argument) {
  (void)argument;
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    esp_err_t result = rtc_sync_from_system();
    if (result == ESP_OK) {
      agenda_schedule_next();
      ESP_LOGI(TAG, "LY|TIME|event=sntp_sync rtc=ok epoch=%lld",
               (long long)time(NULL));
      if (s_post) s_post(APP_EV_TIME_SYNC, 1);
    } else {
      ESP_LOGW(TAG, "LY|TIME|event=sntp_sync rtc=%s", esp_err_to_name(result));
    }
  }
}

static char s_storage_scan_id[24];
static char s_storage_cursor[SD_UPLOAD_SESSION_ID_BYTES];
static int64_t s_storage_scan_complete_ms;

static void storage_scan_reset(void) {
  s_storage_scan_id[0] = '\0';
  s_storage_cursor[0] = '\0';
  s_storage_scan_complete_ms = 0;
}

static esp_err_t storage_command_ack(const char *command_id, const char *status,
                                     cJSON *deleted_ids, uint32_t deleted_count,
                                     uint64_t freed_bytes, const char *error_code) {
  char url[320];
  if (snprintf(url, sizeof(url), "%s/api/v2/device/storage/commands/%s/ack",
               server_base_url(), command_id) >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;
  cJSON *body = cJSON_CreateObject();
  if (!body) return ESP_ERR_NO_MEM;
  cJSON_AddNumberToObject(body, "binding_generation", s_binding_generation);
  cJSON_AddStringToObject(body, "status", status);
  cJSON_AddItemToObject(body, "deleted_session_ids",
                       deleted_ids ? deleted_ids : cJSON_CreateArray());
  cJSON_AddNumberToObject(body, "deleted_count", deleted_count);
  cJSON_AddNumberToObject(body, "freed_bytes", (double)freed_bytes);
  if (error_code) cJSON_AddStringToObject(body, "error_code", error_code);
  else cJSON_AddNullToObject(body, "error_code");
  char *json = cJSON_PrintUnformatted(body);
  if (!json) {
    cJSON_Delete(body);
    return ESP_ERR_NO_MEM;
  }
  response_buffer_t response = {0};
  int http_status = 0;
  esp_err_t error = cloud_request(HTTP_METHOD_POST, url, "application/json", NULL,
                                  json, strlen(json), NULL, NULL,
                                  &response, &http_status);
  cJSON_free(json);
  cJSON_Delete(body);
  if (error == ESP_OK && http_status == 200) return ESP_OK;
  if (http_status == 401 || http_status == 403) auth_repair_binding(http_status);
  return error == ESP_OK ? ESP_FAIL : error;
}

static void storage_execute_command(cJSON *command) {
  cJSON *id = cJSON_GetObjectItemCaseSensitive(command, "command_id");
  cJSON *action = cJSON_GetObjectItemCaseSensitive(command, "action");
  if (!cJSON_IsString(id) || strlen(id->valuestring) > 72 ||
      !cJSON_IsString(action)) return;
  cJSON *deleted_ids = cJSON_CreateArray();
  if (!deleted_ids) return;
  uint32_t deleted_count = 0;
  uint64_t freed = 0;
  const char *status = "completed";
  const char *error_code = NULL;
  bool defer_until_closed = false;
  if (strcmp(action->valuestring, "delete_sessions") == 0) {
    cJSON *ids = cJSON_GetObjectItemCaseSensitive(command, "session_ids");
    if (!cJSON_IsArray(ids) || cJSON_GetArraySize(ids) == 0 ||
        cJSON_GetArraySize(ids) > 32) {
      status = "rejected";
      error_code = "INVALID_SESSION_LIST";
    } else {
      cJSON *entry = NULL;
      cJSON_ArrayForEach(entry, ids) {
        uint64_t one = 0;
        esp_err_t removal = cJSON_IsString(entry)
                              ? sd_storage_delete_local(s_binding_generation,
                                                        entry->valuestring, &one)
                              : ESP_ERR_INVALID_ARG;
        if (removal == ESP_ERR_INVALID_STATE) {
          /* The owner may request deletion at any time.  Keep the server
             command pending while this exact session still has an open WAV;
             the next inventory exchange retries it after safe close. */
          defer_until_closed = true;
          break;
        }
        if (removal != ESP_OK && removal != ESP_ERR_NOT_FOUND) {
          status = "failed";
          error_code = "SESSION_DELETE_FAILED";
          break;
        }
        cJSON_AddItemToArray(deleted_ids, cJSON_CreateString(entry->valuestring));
        deleted_count++;
        freed += one;
      }
    }
  } else if (strcmp(action->valuestring, "delete_all_closed") == 0) {
    if (strcmp(s_bulk_delete_command, id->valuestring) != 0) {
      snprintf(s_bulk_delete_command, sizeof(s_bulk_delete_command), "%s",
               id->valuestring);
      s_bulk_deleted_count = 0;
      s_bulk_freed_bytes = 0;
    }
    uint32_t batch_count = 0;
    uint64_t batch_freed = 0;
    esp_err_t removal = sd_storage_delete_all_local(s_binding_generation,
                                                     &batch_count,
                                                     &batch_freed);
    s_bulk_deleted_count += batch_count;
    s_bulk_freed_bytes += batch_freed;
    deleted_count = s_bulk_deleted_count;
    freed = s_bulk_freed_bytes;
    if (removal == ESP_ERR_INVALID_STATE) {
      defer_until_closed = true;
    } else if (removal != ESP_OK) {
      status = "failed";
      error_code = "BULK_DELETE_FAILED";
    }
  } else {
    status = "rejected";
    error_code = "ACTION_UNSUPPORTED";
  }
  if (defer_until_closed) {
    ESP_LOGI(TAG,
             "LY|STORAGE|command=%s action=%s status=deferred reason=session_open",
             id->valuestring, action->valuestring);
    cJSON_Delete(deleted_ids);
    return;
  }
  esp_err_t ack = storage_command_ack(id->valuestring, status, deleted_ids,
                                      deleted_count, freed, error_code);
  if (ack == ESP_OK && deleted_count) storage_scan_reset();
  ESP_LOGI(TAG,
           "LY|STORAGE|command=%s action=%s status=%s deleted=%lu freed=%llu ack=%s",
           id->valuestring, action->valuestring, status,
           (unsigned long)deleted_count, (unsigned long long)freed,
           esp_err_to_name(ack));
  if (ack == ESP_OK && strcmp(action->valuestring, "delete_all_closed") == 0) {
    s_bulk_delete_command[0] = '\0';
    s_bulk_deleted_count = 0;
    s_bulk_freed_bytes = 0;
  }
}

static uint32_t storage_sync_once(void) {
  int64_t now_ms = esp_timer_get_time() / 1000;
  if (s_storage_scan_complete_ms) {
    /* Keep sending the already-complete scan's empty tail page while the
       inventory itself is in its 60 s quiet period.  The server returns a
       pending storage command on every snapshot, so this decouples command
       pickup (10 s) from the relatively slow full inventory rescan (60 s).
       Reusing the same scan id is safe: all pages from that scan keep their
       scan_id and the repeated complete page contains no stale deletion. */
    if (now_ms - s_storage_scan_complete_ms >= STORAGE_SCAN_PAUSE_MS) {
      storage_scan_reset();
    }
  }
  if (!s_storage_scan_id[0]) {
    snprintf(s_storage_scan_id, sizeof(s_storage_scan_id), "%08lx%08lx",
             (unsigned long)esp_random(), (unsigned long)esp_random());
    s_storage_cursor[0] = '\0';
  }
  sd_storage_session_t sessions[SD_STORAGE_PAGE_MAX] = {0};
  size_t count = 0;
  char next_cursor[SD_UPLOAD_SESSION_ID_BYTES] = {0};
  bool complete = false;
  uint64_t total = 0, free_space = 0;
  esp_err_t error = sd_storage_info(&total, &free_space);
  if (error == ESP_OK) {
    error = sd_storage_inventory_page(s_binding_generation, s_storage_cursor,
                                      sessions, SD_STORAGE_PAGE_MAX, &count,
                                      next_cursor, sizeof(next_cursor), &complete);
  }
  if (error != ESP_OK) return 30000;
  cJSON *body = cJSON_CreateObject();
  cJSON *array = body ? cJSON_AddArrayToObject(body, "sessions") : NULL;
  if (!body || !array) {
    cJSON_Delete(body);
    return 30000;
  }
  cJSON_AddNumberToObject(body, "binding_generation", s_binding_generation);
  cJSON_AddStringToObject(body, "scan_id", s_storage_scan_id);
  cJSON_AddBoolToObject(body, "scan_start", s_storage_cursor[0] == '\0');
  cJSON_AddBoolToObject(body, "complete", complete);
  cJSON_AddNumberToObject(body, "total_bytes", (double)total);
  cJSON_AddNumberToObject(body, "free_bytes", (double)free_space);
  for (size_t i = 0; i < count; ++i) {
    cJSON *entry = cJSON_CreateObject();
    cJSON_AddStringToObject(entry, "client_session_id", sessions[i].session_id);
    cJSON_AddNumberToObject(entry, "local_bytes", (double)sessions[i].local_bytes);
    if (sessions[i].ended_at_utc > 0) {
      cJSON_AddNumberToObject(entry, "ended_at_utc", (double)sessions[i].ended_at_utc);
    } else {
      cJSON_AddNullToObject(entry, "ended_at_utc");
    }
    cJSON_AddItemToArray(array, entry);
  }
  char *json = cJSON_PrintUnformatted(body);
  cJSON_Delete(body);
  if (!json) return 30000;
  char url[256];
  snprintf(url, sizeof(url), "%s/api/v2/device/storage/snapshot", server_base_url());
  response_buffer_t response = {0};
  int http_status = 0;
  error = cloud_request(HTTP_METHOD_PUT, url, "application/json", NULL,
                        json, strlen(json), NULL, NULL, &response, &http_status);
  cJSON_free(json);
  if (error != ESP_OK || http_status != 200) {
    if (http_status == 401 || http_status == 403) auth_repair_binding(http_status);
    return 30000;
  }
  cJSON *root = cJSON_Parse(response.data);
  if (!root) return 30000;
  cJSON *command = cJSON_GetObjectItemCaseSensitive(root, "command");
  if (cJSON_IsObject(command)) storage_execute_command(command);
  cJSON_Delete(root);
  if (s_storage_scan_id[0]) {
    snprintf(s_storage_cursor, sizeof(s_storage_cursor), "%s", next_cursor);
    if (complete) s_storage_scan_complete_ms = now_ms;
  }

  return complete ? STORAGE_SCAN_PAUSE_MS : 1000;
}

static void upload_task(void *argument) {
  upload_task_buffers_t *buffers = (upload_task_buffers_t *)argument;
  uint8_t *audio_buffer = buffers ? buffers->audio : NULL;
  uint8_t *range_buffer = buffers ? buffers->range : NULL;
  uint8_t *marks_buffer = buffers ? buffers->marks : NULL;
  uint8_t *todo_audio = buffers ? buffers->todo_audio : NULL;
  ESP_LOGI(TAG,
           "LY|UPLOAD|event=task_ready stack=psram stack_bytes=%u live=%d range=%d range_mode=serial marks=%d todo=%d",
           (unsigned)UPLOADER_STACK_BYTES, audio_buffer != NULL,
           range_buffer != NULL, marks_buffer != NULL, todo_audio != NULL);
  uint32_t last_sessions = UINT32_MAX;
  bool stack_logged = false;
  int64_t next_upload_ms = 0;
  int64_t next_organizer_ms = 0;
  int64_t next_agenda_ms = 0;
  int64_t next_storage_ms = 0;
  int64_t next_backlog_ms = 0;
  int64_t last_health_ms = 0;
  uint32_t storage_generation = 0;
  uint32_t sessions = 0;
  uint64_t pending_bytes = 0;
  char last_notified[LUOYE_TODO_ID_BYTES] = {0};
  uint32_t last_notified_revision = 0;
  char scheduled_upload_id[SD_UPLOAD_SESSION_ID_BYTES] = {0};
  uint32_t handled_manual_sync_revision = 0;
  for (;;) {
    int64_t now_ms = esp_timer_get_time() / 1000;
    uint32_t requested_revision = s_manual_sync_request_revision;
    if (requested_revision != handled_manual_sync_revision) {
      handled_manual_sync_revision = requested_revision;
      scheduled_upload_id[0] = '\0';
      next_upload_ms = 0;
      ESP_LOGI(TAG,
               "LY|SYNC|state=rearmed revision=%lu next=upload_plan block_bytes=%u",
               (unsigned long)requested_revision,
               (unsigned)RANGE_BLOCK_BYTES);
    }
    bulk_wifi_ps_update(s_manual_sync && s_online);
    /* A latched storage fault is a hard runtime boundary.  Do not scan the
       filesystem again just to refresh backlog counters: keep the last known
       values and wait for a reboot to perform a clean card initialization. */
    if (storage_sd_faulted()) {
      scheduled_upload_id[0] = '\0';
      stop_for_storage_fault("scheduler", ESP_FAIL);
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    if (now_ms >= next_backlog_ms) {
      esp_err_t backlog_result = sd_upload_backlog(&sessions, &pending_bytes);
      if (backlog_result == ESP_OK || backlog_result == ESP_ERR_NOT_FOUND) {
        uint64_t backlog_s64 = pending_bytes ?
            (pending_bytes + 31999U) / 32000U : (sessions ? 1U : 0U);
        int32_t backlog_s =
            (int32_t)(backlog_s64 > 65535U ? 65535U : backlog_s64);
        if (s_post) s_post(APP_EV_BACKLOG, backlog_s);
        if (sessions != last_sessions) {
          ESP_LOGI(TAG,
                   "LY|UPLOAD|event=backlog sessions=%lu pending_bytes=%llu",
                   (unsigned long)sessions, (unsigned long long)pending_bytes);
          last_sessions = sessions;
        }
      } else {
        ESP_LOGE(TAG,
                 "LY|UPLOAD|event=backlog result=scan_failed esp=%s keep_last=1",
                 esp_err_to_name(backlog_result));
        stop_manual_sync_scan_error("backlog", backlog_result);
      }
      next_backlog_ms = now_ms + 3000;
    }
    if (s_live_gap_signal && s_bound && s_live_session_id[0]) {
      sd_upload_item_t gap_item;
      esp_err_t found = sd_upload_current(s_binding_generation, &gap_item);
      if (found != ESP_OK) {
        found = sd_upload_find(s_binding_generation, s_live_session_id,
                               &gap_item);
      }
      if (found == ESP_OK) {
        mark_live_gap(&gap_item, "transport_offline");
        s_live_gap_signal = false;
      }
    }
    bool network_action = false;
    if (s_idle_agenda_maintenance) {
      /* Silent standby maintenance owns an isolated network lane.  No live,
         history, todo-audio or storage request may start in this mode. */
      if (!s_idle_agenda_done && s_online && s_bound && !s_pair_requested &&
          s_contract_checked && s_cloud_ready) {
        ESP_LOGD(TAG, "LY|NETSCHED|lane=N6_AGENDA mode=standby_maintenance");
        s_uploader_busy = true;
        agenda_sync_once(false);
        s_uploader_busy = false;
        s_idle_agenda_done = true;
        network_action = true;
      }
    } else if (!s_idle_suspended && !s_idle_resuming &&
        s_online && s_bound && !s_pair_requested &&
        s_contract_checked && s_cloud_ready &&
        audio_buffer && range_buffer && marks_buffer) {
      sd_upload_item_t item;
      bool live_lane = sd_upload_current(s_binding_generation, &item) == ESP_OK;
      if (!live_lane && s_live_session_id[0]) {
        live_lane = sd_upload_find(s_binding_generation, s_live_session_id,
                                   &item) == ESP_OK;
        if (!live_lane) {
          ESP_LOGI(TAG, "LY|UPLOAD|live_lane=released id=%s",
                   s_live_session_id);
          s_live_session_id[0] = '\0';
        }
      }
      if (live_lane && item.local_closed && item.deferred_gaps &&
          !s_manual_sync &&
          (!item.remote_session_created || item.defer_acked)) {
        ESP_LOGI(TAG,
                 "LY|UPLOAD|live_lane=deferred id=%s manual_sync=required",
                 item.session_id);
        s_live_session_id[0] = '\0';
        live_lane = false;
      }
      bool have_item = live_lane;
      esp_err_t history_scan = ESP_ERR_NOT_FOUND;
      if (!have_item && s_manual_sync) {
        history_scan = sd_upload_next(s_binding_generation, &item);
        have_item = history_scan == ESP_OK;
        if (!have_item && history_scan != ESP_ERR_NOT_FOUND) {
          scheduled_upload_id[0] = '\0';
          if (storage_sd_faulted()) {
            stop_for_storage_fault("select_history", history_scan);
          } else {
            stop_manual_sync_scan_error("select_history", history_scan);
          }
        }
      }
      if (storage_sd_faulted()) {
        scheduled_upload_id[0] = '\0';
        stop_for_storage_fault("select", ESP_FAIL);
        vTaskDelay(pdMS_TO_TICKS(250));
        continue;
      }
      if (have_item && strcmp(scheduled_upload_id, item.session_id) != 0) {
        snprintf(scheduled_upload_id, sizeof(scheduled_upload_id), "%s",
                 item.session_id);
        next_upload_ms = 0;
        if (live_lane) s_next_live_poll_ms = 0;
      } else if (!have_item) {
        scheduled_upload_id[0] = '\0';
      }
      if (have_item && now_ms >= next_upload_ms) {
        const char *lane = live_lane ? "N1_LIVE" : "N4_HISTORY";
        ESP_LOGD(TAG, "LY|NETSCHED|lane=%s id=%s state=%s mode=%s",
                 lane, item.session_id, item.state, item.upload_mode);
        s_uploader_busy = true;
        uint32_t delay = process_upload_item(&item, audio_buffer, marks_buffer,
                                             range_buffer);
        s_uploader_busy = false;
        next_upload_ms = now_ms + (delay < 20 ? 20 : delay);
        network_action = true;
        if (!stack_logged) {
          stack_logged = true;
          ESP_LOGI(TAG, "LY|STACK|task=uploader free=%u unit_bytes=%u",
                   (unsigned)uxTaskGetStackHighWaterMark(NULL),
                   (unsigned)sizeof(StackType_t));
        }
      } else if (s_manual_sync && !s_live_session_id[0]) {
        if (!have_item && history_scan == ESP_ERR_NOT_FOUND) {
          /* A writer or scanner may latch FAULTED after the selection gate.
             Never turn that narrow race into a false manual-sync success. */
          if (storage_sd_faulted()) {
            stop_for_storage_fault("complete_gate", ESP_FAIL);
          } else {
            s_manual_sync = false;
            if (s_post) s_post(APP_EV_SYNC_CHANGE, APP_SYNC_DONE);
            ESP_LOGI(TAG, "LY|SYNC|state=complete local_queue=empty");
          }
        }
      }

      if (storage_generation != s_binding_generation) {
        storage_generation = s_binding_generation;
        storage_scan_reset();
        next_storage_ms = 0;
        next_agenda_ms = 0;
      }

      /* One task owns every authenticated cloud request.  Lower lanes run
         only while there is no recording/finalize/history work to preempt. */
      bool idle_cloud_lane = !have_item && !sd_session_is_open() &&
                             !s_manual_sync && !s_live_session_id[0];
      if (!network_action && idle_cloud_lane && s_agenda_sync_requested) {
        /* A visible-page/reconnect/binding request is immediate at the next
           safe cloud slot, but it still cannot preempt recording or upload. */
        ESP_LOGD(TAG, "LY|NETSCHED|lane=N6_AGENDA reason=forced");
        s_uploader_busy = true;
        uint32_t delay = agenda_sync_once(true);
        s_uploader_busy = false;
        s_agenda_sync_requested = false;
        next_agenda_ms = now_ms + (delay < AGENDA_POLL_MS
                                     ? AGENDA_POLL_MS : delay);
        next_organizer_ms = now_ms + 100;
        network_action = true;
      } else if (!network_action && idle_cloud_lane &&
                 now_ms >= next_organizer_ms) {
        luoye_todo_item_t todo;
        if (todo_audio && todo_next(s_binding_generation, &todo) == ESP_OK) {
          ESP_LOGD(TAG, "LY|NETSCHED|lane=N5_TODO id=%s", todo.id);
          s_uploader_busy = true;
          uint32_t delay = process_todo(&todo, todo_audio, TODO_AUDIO_BYTES);
          s_uploader_busy = false;
          next_organizer_ms = now_ms + (delay < 100 ? 100 : delay);
          network_action = true;
        } else if (now_ms >= next_agenda_ms) {
          ESP_LOGD(TAG, "LY|NETSCHED|lane=N6_AGENDA");
          s_uploader_busy = true;
          uint32_t delay = agenda_sync_once(true);
          s_uploader_busy = false;
          next_agenda_ms = now_ms + (delay < AGENDA_POLL_MS
                                       ? AGENDA_POLL_MS : delay);
          next_organizer_ms = now_ms + 100;
          network_action = true;
        }
      }
      if (!network_action && idle_cloud_lane && now_ms >= next_storage_ms) {
        ESP_LOGD(TAG, "LY|NETSCHED|lane=N7_STORAGE");
        s_uploader_busy = true;
        uint32_t delay = storage_sync_once();
        s_uploader_busy = false;
        next_storage_ms = now_ms + (delay < 1000 ? 1000 : delay);
        network_action = true;
      }

      luoye_todo_item_t latest;
      if (todo_latest(&latest) &&
          latest.binding_generation == s_binding_generation &&
          latest.state == LUOYE_TODO_NEEDS_CONFIRMATION &&
          (strcmp(last_notified, latest.id) != 0 ||
           last_notified_revision != latest.result_revision)) {
        snprintf(last_notified, sizeof(last_notified), "%s", latest.id);
        last_notified_revision = latest.result_revision;
        if (s_post) s_post(APP_EV_TODO_RESULT, 1);
      }
      if (now_ms - last_health_ms >= 600000 || last_health_ms == 0) {
        last_health_ms = now_ms;
        ESP_LOGI(TAG,
                 "LY|HEALTH|task=uploader heap=%lu largest=%lu psram=%lu stack_free=%u backlog=%lu",
                 (unsigned long)esp_get_free_heap_size(),
                 (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                 (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                 (unsigned)uxTaskGetStackHighWaterMark(NULL),
                 (unsigned long)sessions);
      }
    }
    /* Manual history sync is the bulk-upload lifetime. Restore the exact
       pre-upload power-save mode immediately after its queue completes. */
    bulk_wifi_ps_update(s_manual_sync && s_online);
    vTaskDelay(pdMS_TO_TICKS(network_action ? 20 : 100));
  }
}

static void reconnect_cb(void *argument) {
  (void)argument;
  if ((!s_idle_suspended || s_idle_resuming) && s_have_credentials &&
      !s_pending_credentials) {
    wifi_selector_notify();
  }
}

static void wifi_event(void *argument, esp_event_base_t base, int32_t id, void *data) {
  (void)argument;
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    if (s_offline_since_ms <= 0) s_offline_since_ms = esp_timer_get_time() / 1000;
    if ((!s_idle_suspended || s_idle_resuming) && s_have_credentials &&
        !s_pending_credentials) {
      wifi_selector_notify();
    }
    return;
  }

  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    s_wifi_connecting = false;
    if (s_offline_since_ms <= 0) s_offline_since_ms = esp_timer_get_time() / 1000;
    s_contract_checked = false;
    if (s_live_session_id[0]) s_live_gap_signal = true;
    cloud_set_ready(false);
    s_use_lan_server = false;
    if (s_online) {
      s_online = false;
      if (s_post) s_post(APP_EV_NET_CHANGE, 0);
    }
    if (s_idle_suspended && !s_idle_resuming) {
      ESP_LOGD(TAG, "LY|IDLE_NET|event=disconnect reason=intentional_suspend");
      return;
    }
    if (s_pending_credentials) {
      if (s_pending_attempts == 0) {
        s_pending_attempts = 1;
        esp_wifi_connect();
      } else {
        s_pending_credentials = false;
        s_station_config_is_candidate = false;
        pair_set_state(NET_PAIR_ERROR, ESP_FAIL, 0);
        wifi_selector_notify();
      }
      return;
    }
    s_failed_wifi_profile = s_active_wifi_profile;
    if (s_have_credentials && (!s_idle_suspended || s_idle_resuming)) {
      wifi_retry_schedule();
    }
    return;
  }

  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    s_wifi_connecting = false;
    s_offline_since_ms = 0;
    s_contract_checked = false;
    cloud_set_ready(false);
    const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
    const bool resumed_from_idle = s_idle_resuming;
    if (resumed_from_idle) {
      s_idle_resuming = false;
      /* Agenda maintenance keeps the suspended ownership flag asserted so
         the normal uploader lanes remain closed until Wi-Fi is stopped. */
      if (!s_idle_agenda_maintenance) s_idle_suspended = false;
      if (s_reconnect_timer) esp_timer_stop(s_reconnect_timer);
      ESP_LOGI(TAG,
               "LY|IDLE_NET|state=%s result=ESP_OK ip=" IPSTR,
               s_idle_agenda_maintenance ? "maintenance_connected" : "resumed",
               IP2STR(&event->ip_info.ip));
    }
    wifi_ap_record_t ap = {0};
    esp_err_t ap_error = esp_wifi_sta_get_ap_info(&ap);
    const char *fallback_ssid = s_pending_credentials ? s_pending_ssid : s_saved_ssid;
    const char *ssid = ap_error == ESP_OK ? (const char *)ap.ssid : fallback_ssid;
    s_use_lan_server = strcmp(ssid, LAN_WIFI_SSID) == 0;
    ESP_LOGI(TAG,
             "LY|WIFI|ssid=%s ip=" IPSTR " rssi=%d route=%s server=%s",
             ssid, IP2STR(&event->ip_info.ip),
             ap_error == ESP_OK ? ap.rssi : 0,
             s_use_lan_server ? "lan" : "public", server_base_url());
    s_online = true;
    s_failed_wifi_profile = -1;
    if (s_post) s_post(APP_EV_NET_CHANGE, 1);
    if (!s_idle_agenda_maintenance) s_agenda_sync_requested = true;
    if (s_pending_credentials) {
      esp_err_t error = save_credentials(s_pending_ssid, s_pending_pass);
      if (error != ESP_OK) {
        pair_set_state(NET_PAIR_ERROR, error, 0);
        return;
      }
      strlcpy(s_saved_ssid, s_pending_ssid, sizeof(s_saved_ssid));
      strlcpy(s_saved_pass, s_pending_pass, sizeof(s_saved_pass));
      s_have_credentials = true;
      s_active_wifi_profile = 0;
      s_pending_credentials = false;
      s_station_config_is_candidate = false;
      s_pair_requested = !s_bound;
      memset(s_pending_pass, 0, sizeof(s_pending_pass));
      pair_set_state(s_bound ? NET_PAIR_BOUND : NET_PAIR_WIFI_CONNECTED,
                     ESP_OK, 0);
    } else if (s_pair_requested && !s_bound) {
      pair_set_state(NET_PAIR_WIFI_CONNECTED, ESP_OK, 0);
    }
  }
}

esp_err_t net_uploader_init(net_post_fn post) {
  s_post = post;
  s_pair_lock = xSemaphoreCreateMutex();
  s_live_lock = xSemaphoreCreateMutex();
  if (!s_pair_lock || !s_live_lock) return ESP_ERR_NO_MEM;

  uint8_t mac[6];
  esp_err_t error = esp_read_mac(mac, ESP_MAC_WIFI_STA);
  if (error != ESP_OK) return error;
  snprintf(s_pair.device_id, sizeof(s_pair.device_id),
           "LY-%02X%02X%02X%02X%02X%02X",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  snprintf(s_pair.ap_ssid, sizeof(s_pair.ap_ssid),
           "LUOYE-%02X%02X", mac[4], mac[5]);
  load_nvs_config();
  /* A token may be awaiting same-account repair after a 401/403.  Retain the
     cached agenda under its last confirmed generation; pair/status will clear
     it only if the server later confirms a different generation. */
  agenda_reset_binding(s_binding_generation);
  s_pair.state = NET_PAIR_IDLE;
  const esp_timer_create_args_t reconnect_args = {
    .callback = reconnect_cb,
    .name = "wifi_reconn",
  };
  error = esp_timer_create(&reconnect_args, &s_reconnect_timer);
  if (error != ESP_OK) return error;

  error = esp_netif_init();
  if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) return error;
  error = esp_event_loop_create_default();
  if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) return error;
  s_sta_netif = esp_netif_create_default_wifi_sta();
  if (!s_sta_netif) return ESP_ERR_NO_MEM;

  if (xTaskCreate(time_sync_task, "time_sync", 4096, NULL, 7,
                  &s_time_task) != pdPASS) return ESP_ERR_NO_MEM;
  esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
  sntp.sync_cb = sntp_sync_cb;
  error = esp_netif_sntp_init(&sntp);
  if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) return error;

  wifi_init_config_t wifi_init = WIFI_INIT_CONFIG_DEFAULT();
  error = esp_wifi_init(&wifi_init);
  if (error != ESP_OK) return error;
  error = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL);
  if (error != ESP_OK) return error;
  error = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL);
  if (error != ESP_OK) return error;

  if (xTaskCreate(wifi_selector_task, "wifi_select", 6144, NULL, 9,
                  &s_wifi_selector_task) != pdPASS) return ESP_ERR_NO_MEM;

  if (s_have_credentials) {
    wifi_config_t station = {0};
    strlcpy((char *)station.sta.ssid, s_saved_ssid, sizeof(station.sta.ssid));
    strlcpy((char *)station.sta.password, s_saved_pass,
            sizeof(station.sta.password));
    station.sta.pmf_cfg.capable = true;
    error = esp_wifi_set_mode(WIFI_MODE_STA);
    if (error == ESP_OK) error = esp_wifi_set_config(WIFI_IF_STA, &station);
    if (error == ESP_OK) error = esp_wifi_start();
    if (error != ESP_OK) return error;
    s_wifi_started = true;
  } else {
    ESP_LOGW(TAG, "LY|NET|state=offline reason=no_credentials local_recording=enabled");
  }

  ESP_LOGI(TAG, "LY|IDENTITY|device=%s source=efuse_mac bound=%d",
           s_pair.device_id, s_bound);
  /* Reserve every long-lived upload buffer before publishing a healthy
     network subsystem. This prevents a task that exists but can never service
     the durable SD queue. The todo buffer is optional; audio/range/marks are
     required for recording upload. */
  const uint32_t upload_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
  s_upload_buffers.audio = heap_caps_malloc(CHUNK_BYTES, upload_caps);
  s_upload_buffers.range = heap_caps_malloc(RANGE_STREAM_BYTES, upload_caps);
  s_upload_buffers.marks = heap_caps_malloc(MARKS_BUFFER_BYTES, upload_caps);
  s_upload_buffers.todo_audio = heap_caps_malloc(TODO_AUDIO_BYTES, upload_caps);
  if (!s_upload_buffers.audio || !s_upload_buffers.range ||
      !s_upload_buffers.marks) {
    ESP_LOGE(TAG,
             "LY|UPLOAD|event=buffer_alloc_failed live=%d range=%d marks=%d todo=%d psram_free=%lu",
             s_upload_buffers.audio != NULL, s_upload_buffers.range != NULL,
             s_upload_buffers.marks != NULL, s_upload_buffers.todo_audio != NULL,
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    upload_buffers_release();
    return ESP_ERR_NO_MEM;
  }

  TaskHandle_t pairing_handle = NULL;
  if (xTaskCreate(pairing_task, "pairing", 14336, NULL, 8,
                  &pairing_handle) != pdPASS) {
    ESP_LOGE(TAG, "LY|NET|event=task_create_failed task=pairing stack=internal");
    upload_buffers_release();
    return ESP_ERR_NO_MEM;
  }
  /* Storage snapshots combine FAT traversal, cJSON and the HTTP/TLS call
     chain. 16 KiB overflowed reproducibly, while a 32 KiB internal stack no
     longer fits after Wi-Fi initialization. Keep the proven stack size but
     place it in PSRAM; all SD SPI DMA still uses its reserved internal stage. */
  if (xTaskCreateWithCaps(upload_task, "uploader", UPLOADER_STACK_BYTES,
                          &s_upload_buffers, 8, NULL,
                          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
    vTaskDelete(pairing_handle);
    ESP_LOGE(TAG,
             "LY|NET|event=task_create_failed task=uploader stack=psram stack_bytes=%u psram_largest=%lu",
             (unsigned)UPLOADER_STACK_BYTES,
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
    upload_buffers_release();
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}
