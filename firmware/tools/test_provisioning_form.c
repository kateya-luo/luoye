#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../components/net_uploader/include/provisioning_form.h"

int main(void) {
  char value[65];

  assert(provisioning_form_value("ssid=Meeting+Room&password=12345678",
                                 "ssid", value, sizeof(value)));
  assert(strcmp(value, "Meeting Room") == 0);

  assert(provisioning_form_value(
    "ssid=%E4%BC%9A%E8%AE%AE%E5%AE%A4&password=abcdefgh",
    "ssid", value, sizeof(value)));
  assert(strcmp(value, "会议室") == 0);

  assert(!provisioning_form_value("ssid=bad%2", "ssid", value, sizeof(value)));
  assert(provisioning_form_value("ssid=Open&password=", "password",
                                 value, sizeof(value)));
  assert(strcmp(value, "") == 0);
  assert(!provisioning_form_value("ssid=Home&password=bad%2", "password",
                                  value, sizeof(value)));
  assert(!provisioning_form_value("password=12345678", "ssid",
                                  value, sizeof(value)));
  assert(provisioning_form_value(
      "ssid=Home&password=12345678&client_time_utc=1785547425",
      "client_time_utc", value, sizeof(value)));
  assert(strcmp(value, "1785547425") == 0);

  assert(provisioning_credentials_valid("OpenWiFi", ""));
  assert(provisioning_credentials_valid("Home", "12345678"));
  assert(!provisioning_credentials_valid("", "12345678"));
  assert(!provisioning_credentials_valid("Home", "short"));

  char long_ssid[34];
  memset(long_ssid, 'A', sizeof(long_ssid) - 1);
  long_ssid[sizeof(long_ssid) - 1] = '\0';
  assert(!provisioning_credentials_valid(long_ssid, "12345678"));

  assert(provisioning_pair_restart_required(409, "PAIRING_CODE_IN_USE"));
  assert(provisioning_pair_restart_required(410, "PAIRING_EXPIRED"));
  assert(provisioning_pair_restart_required(404, "PAIRING_NOT_FOUND"));
  assert(provisioning_pair_restart_required(409, "PAIRING_NOT_ACTIVE"));
  assert(!provisioning_pair_restart_required(409, "PAIRING_EXPIRED"));
  assert(!provisioning_pair_restart_required(410, "PAIRING_CODE_IN_USE"));
  assert(!provisioning_pair_restart_required(409, "OTHER_CONFLICT"));
  assert(!provisioning_pair_restart_required(500, "PAIRING_EXPIRED"));
  assert(!provisioning_pair_restart_required(409, NULL));
  assert(provisioning_auth_repair_required(401));
  assert(provisioning_auth_repair_required(403));
  assert(!provisioning_auth_repair_required(404));
  assert(!provisioning_auth_repair_required(409));
  assert(!provisioning_auth_repair_required(500));

  int64_t epoch = 0;
  assert(provisioning_parse_utc_iso8601("2020-01-01T00:00:00Z", &epoch));
  assert(epoch == 1577836800LL);
  assert(provisioning_parse_utc_iso8601(
      "2026-08-01T01:23:45.123456+00:00", &epoch));
  assert(epoch == 1785547425LL);
  assert(!provisioning_parse_utc_iso8601("2026-02-30T01:23:45+00:00", &epoch));
  assert(!provisioning_parse_utc_iso8601("2026-08-01T01:23:45+08:00", &epoch));
  assert(provisioning_parse_client_unix_utc("1577836800", &epoch));
  assert(epoch == 1577836800LL);
  assert(provisioning_parse_client_unix_utc("4102444799", &epoch));
  assert(epoch == 4102444799LL);
  assert(!provisioning_parse_client_unix_utc("1577836799", &epoch));
  assert(!provisioning_parse_client_unix_utc("4102444800", &epoch));
  assert(!provisioning_parse_client_unix_utc("+1785547425", &epoch));
  assert(!provisioning_parse_client_unix_utc("1785547425.0", &epoch));
  assert(!provisioning_parse_client_unix_utc(" 1785547425", &epoch));
  assert(!provisioning_parse_client_unix_utc("999999999999999999999", &epoch));
  assert(!provisioning_parse_client_unix_utc("", &epoch));
  assert(provisioning_https_clock_ready(1577836800LL));
  assert(provisioning_https_clock_ready(4102444799LL));
  assert(!provisioning_https_clock_ready(0));
  assert(!provisioning_https_clock_ready(4102444800LL));
  assert(provisioning_clock_bootstrap_required(0, 1785547425LL));
  /* An already-valid RTC is authoritative; browser/server hints cannot
     overwrite it. */
  assert(!provisioning_clock_bootstrap_required(1577836800LL, 1785547425LL));
  assert(!provisioning_clock_bootstrap_required(1700000000LL, 1785547425LL));
  assert(!provisioning_clock_bootstrap_required(0, 123));
  assert(!provisioning_clock_bootstrap_required(0, 4102444800LL));

  puts("provisioning-form checks passed");
  return 0;
}
