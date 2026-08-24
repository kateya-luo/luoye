#include "provisioning_form.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

static bool decode(const char *source, size_t length, char *out, size_t out_size) {
  size_t written = 0;
  if (!source || !out || out_size == 0) return false;
  for (size_t i = 0; i < length; i++) {
    unsigned char value = (unsigned char)source[i];
    if (value == '+') {
      value = ' ';
    } else if (value == '%') {
      if (i + 2 >= length ||
          !isxdigit((unsigned char)source[i + 1]) ||
          !isxdigit((unsigned char)source[i + 2])) {
        return false;
      }
      char hex[3] = {source[i + 1], source[i + 2], '\0'};
      value = (unsigned char)strtoul(hex, NULL, 16);
      i += 2;
    }
    if (value == '\0' || written + 1 >= out_size) return false;
    out[written++] = (char)value;
  }
  out[written] = '\0';
  return true;
}

bool provisioning_form_value(const char *body, const char *name,
                             char *out, size_t out_size) {
  if (!body || !name || !out || out_size == 0) return false;
  size_t name_length = strlen(name);
  const char *cursor = body;
  while (*cursor) {
    const char *end = strchr(cursor, '&');
    if (!end) end = cursor + strlen(cursor);
    const char *equals = memchr(cursor, '=', (size_t)(end - cursor));
    if (equals && (size_t)(equals - cursor) == name_length &&
        memcmp(cursor, name, name_length) == 0) {
      return decode(equals + 1, (size_t)(end - equals - 1), out, out_size);
    }
    cursor = *end ? end + 1 : end;
  }
  out[0] = '\0';
  return false;
}

bool provisioning_credentials_valid(const char *ssid, const char *password) {
  if (!ssid || !password) return false;
  size_t ssid_length = strlen(ssid);
  size_t password_length = strlen(password);
  return ssid_length >= 1 && ssid_length <= 32 &&
         (password_length == 0 ||
          (password_length >= 8 && password_length <= 63));
}

bool provisioning_pair_restart_required(int http_status,
                                         const char *error_code) {
  if (!error_code) return false;
  return (http_status == 409 &&
          (strcmp(error_code, "PAIRING_CODE_IN_USE") == 0 ||
           strcmp(error_code, "PAIRING_NOT_ACTIVE") == 0)) ||
         (http_status == 410 &&
          strcmp(error_code, "PAIRING_EXPIRED") == 0) ||
         (http_status == 404 &&
          strcmp(error_code, "PAIRING_NOT_FOUND") == 0);
}

bool provisioning_auth_repair_required(int http_status) {
  return http_status == 401 || http_status == 403;
}

static bool decimal_pair(const char *text, int *value) {
  if (!text || !isdigit((unsigned char)text[0]) ||
      !isdigit((unsigned char)text[1])) return false;
  *value = (text[0] - '0') * 10 + (text[1] - '0');
  return true;
}

static bool leap_year(int year) {
  return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
}

bool provisioning_parse_utc_iso8601(const char *text, int64_t *epoch_seconds) {
  if (!text || !epoch_seconds || strlen(text) < 20 ||
      text[4] != '-' || text[7] != '-' || text[10] != 'T' ||
      text[13] != ':' || text[16] != ':') return false;
  for (size_t i = 0; i < 4; ++i) {
    if (!isdigit((unsigned char)text[i])) return false;
  }
  int year = (text[0] - '0') * 1000 + (text[1] - '0') * 100 +
             (text[2] - '0') * 10 + (text[3] - '0');
  int month, day, hour, minute, second;
  if (!decimal_pair(text + 5, &month) || !decimal_pair(text + 8, &day) ||
      !decimal_pair(text + 11, &hour) || !decimal_pair(text + 14, &minute) ||
      !decimal_pair(text + 17, &second)) return false;
  static const int DAYS[] = {31, 28, 31, 30, 31, 30,
                             31, 31, 30, 31, 30, 31};
  if (year < 1970 || year > 2100 || month < 1 || month > 12 || day < 1 ||
      day > DAYS[month - 1] + (month == 2 && leap_year(year)) ||
      hour > 23 || minute > 59 || second > 59) return false;

  const char *zone = text + 19;
  if (*zone == '.') {
    zone++;
    const char *fraction = zone;
    while (isdigit((unsigned char)*zone)) zone++;
    if (zone == fraction) return false;
  }
  if (!((*zone == 'Z' && zone[1] == '\0') ||
        (strcmp(zone, "+00:00") == 0))) return false;

  int adjusted_year = year - (month <= 2);
  int era = adjusted_year / 400;
  unsigned yoe = (unsigned)(adjusted_year - era * 400);
  unsigned shifted_month = (unsigned)(month + (month > 2 ? -3 : 9));
  unsigned doy = (153U * shifted_month + 2U) / 5U + (unsigned)day - 1U;
  unsigned doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
  int64_t days = (int64_t)era * 146097 + doe - 719468;
  *epoch_seconds = days * 86400 + hour * 3600 + minute * 60 + second;
  return true;
}

bool provisioning_parse_client_unix_utc(const char *text,
                                        int64_t *epoch_seconds) {
  if (!text || !epoch_seconds || !text[0]) return false;
  int64_t value = 0;
  for (const unsigned char *cursor = (const unsigned char *)text;
       *cursor; ++cursor) {
    if (!isdigit(*cursor)) return false;
    int digit = *cursor - '0';
    if (value > (INT64_MAX - digit) / 10) return false;
    value = value * 10 + digit;
  }
  if (!provisioning_https_clock_ready(value)) return false;
  *epoch_seconds = value;
  return true;
}

bool provisioning_https_clock_ready(int64_t local_epoch_seconds) {
  const int64_t YEAR_2020 = 1577836800LL;
  const int64_t YEAR_2100 = 4102444800LL;
  return local_epoch_seconds >= YEAR_2020 &&
         local_epoch_seconds < YEAR_2100;
}

bool provisioning_clock_bootstrap_required(int64_t local_epoch_seconds,
                                            int64_t server_epoch_seconds) {
  return local_epoch_seconds < 1577836800LL &&
         provisioning_https_clock_ready(server_epoch_seconds);
}
