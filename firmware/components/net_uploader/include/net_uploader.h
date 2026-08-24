// net_uploader.h — WiFi STA + SoftAP provisioning + account claim status.
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "app_state.h"
#include "live_protocol.h"

typedef void (*net_post_fn)(app_event_t ev, int32_t arg);

typedef enum {
  NET_PAIR_IDLE = 0,
  NET_PAIR_AP_READY,
  NET_PAIR_WIFI_CONNECTING,
  NET_PAIR_WIFI_CONNECTED,
  NET_PAIR_CLAIM_PENDING,
  NET_PAIR_BOUND,
  NET_PAIR_ERROR,
} net_pair_state_t;

typedef struct {
  net_pair_state_t state;
  char ap_ssid[33];
  char ap_password[65];
  char device_id[40];
  char pairing_code[8];
  char masked_account[48];
  esp_err_t last_error;
  int last_http_status;
} net_pairing_info_t;

esp_err_t net_uploader_init(net_post_fn post);
void net_session_begin(const char *session_id, app_scene_t scene, const char *title);
void net_enter_pairing(void);
void net_exit_pairing(void);
bool net_is_online(void);
bool net_is_bound(void);
bool net_is_cloud_ready(void);
/* User-confirmed historical upload.  The active recording lane remains live;
 * closed backlog is processed only while this request is active. */
bool net_request_manual_sync(void);
/* Standby power policy.  Suspend stops the Wi-Fi radio without discarding
 * credentials or binding.  Resume is asynchronous: saved networks are
 * rescanned every five seconds and the suspended state is cleared only after
 * the station has obtained an IP address. */
bool net_can_idle(void);
esp_err_t net_idle_suspend(void);
esp_err_t net_idle_resume(void);
bool net_idle_is_suspended(void);
bool net_idle_agenda_maintenance_start(void);
bool net_idle_agenda_maintenance_done(bool *changed);
esp_err_t net_idle_agenda_maintenance_stop(void);
bool net_request_agenda_sync(void);
uint32_t net_binding_generation(void);
bool net_live_snapshot(luoye_live_result_t *out);
void net_get_pairing_info(net_pairing_info_t *out);
