#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Decodes one application/x-www-form-urlencoded field. The destination is
// always NUL-terminated on success. Malformed percent escapes are rejected.
bool provisioning_form_value(const char *body, const char *name,
                             char *out, size_t out_size);

// ESP32 WiFi limits: SSID 1..32 bytes; password is open (0 bytes) or a
// WPA/WPA2 passphrase of 8..63 bytes.
bool provisioning_credentials_valid(const char *ssid, const char *password);

// Pairing failures are recoverable only when both the HTTP status and the
// structured API error code identify a stale/colliding challenge.  Keeping
// this policy pure makes it host-testable and prevents broad 409/410 retries.
bool provisioning_pair_restart_required(int http_status,
                                         const char *error_code);

// Authentication loss requires a new device claim, while ordinary object
// 404/409 responses remain scoped to that object.
bool provisioning_auth_repair_required(int http_status);

// Trusted server time is used only to bootstrap an obviously invalid local
// clock.  The parser accepts UTC ISO-8601 with optional fractional seconds.
bool provisioning_parse_utc_iso8601(const char *text, int64_t *epoch_seconds);

// The SoftAP page submits the browser's current Unix UTC seconds so a blank
// board can validate the first HTTPS certificate before SNTP is available.
// Only strict decimal values in 2020..2099 are accepted.
bool provisioning_parse_client_unix_utc(const char *text,
                                        int64_t *epoch_seconds);

// HTTPS must not start with the ESP32 default 1970 clock.  This is separate
// from bootstrap policy so an already-valid RTC can never be overwritten by
// a browser value.
bool provisioning_https_clock_ready(int64_t local_epoch_seconds);
bool provisioning_clock_bootstrap_required(int64_t local_epoch_seconds,
                                            int64_t server_epoch_seconds);
