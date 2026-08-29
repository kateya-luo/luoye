// Luoye 1.54-inch UI renderer: approved 200x200 artwork + live data overlays.
#include "ui_render.h"
#include "ui_font.h"
#include "luoye_build_info.h"
#include "net_uploader.h"
#include "agenda_todo.h"
#include "epd_ssd1681.h"
#include "ui154_layout_generated.h"

#include <stdio.h>
#include <string.h>
#include <time.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define DEFAULT_UI_TIMEZONE_OFFSET_MINUTES 480
/* SSD1681 can miss a tiny glyph-only auto-diff after panel sleep.  Keep one
   stable, proven-size window for both dynamic home fields instead: battery
   (158,4,40,17) and clock (27,27,147,52). */
#define HOME_CLOCK_BATTERY_X 24
#define HOME_CLOCK_BATTERY_Y 2
#define HOME_CLOCK_BATTERY_WIDTH 174
#define HOME_CLOCK_BATTERY_HEIGHT 78

static const char *TAG = "ui154";
static uint8_t s_fb[EPD_FB_BYTES];              // packed 1bpp: 0=white, 1=black
static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static volatile int s_pending = -1;
static volatile bool s_busy;
static bool s_assets_ready;
static bool s_have_panel_screen;
static uint32_t s_panel_screen;

/* Stable identity of the visual page, intentionally excluding values that can
   change inside the same page (clock, battery, network and upload status).
   This is the final guard against an asynchronous partial request painting a
   different page over the previous panel image. */
static uint32_t screen_identity(const app_state_t *state) {
  if (!state) return 0;
  if (state->overlay != APP_OV_NONE) {
    return 0x10000U | (uint32_t)state->overlay;
  }
  switch (state->mode) {
    case APP_MODE_STANDBY:
      if (state->page == 0 && state->charging != APP_CHG_NONE) return 0x20010U;
      if (state->page == 1) {
        return 0x20020U | ((uint32_t)state->agenda_page << 8);
      }
      if (state->page == 2) return 0x20030U;
      return 0x20000U;
    case APP_MODE_STARTING: return 0x30000U;
    case APP_MODE_RECORDING:
      if (state->locked) return 0x40010U;
      if (state->paused) return 0x40020U;
      if ((state->page & 1U) != 0) return 0x40030U;
      return 0x40000U;
    case APP_MODE_CLOSING: return 0x50000U;
    case APP_MODE_ENDING: return 0x50010U;
    case APP_MODE_STORAGE_ERROR:
      return 0x60000U | (uint32_t)state->error;
    case APP_MODE_PAIRING:
      return 0x70000U | (uint32_t)state->pairing;
    case APP_MODE_OFF: return 0x80000U;
    default: return 0x90000U | (uint32_t)state->mode;
  }
}

static bool home_clock_active(const app_state_t *state) {
  return state && state->mode == APP_MODE_STANDBY &&
         state->overlay == APP_OV_NONE && state->page == 0 &&
         state->charging == APP_CHG_NONE;
}

static int64_t ui_clock_seconds(void) {
  time_t now = time(NULL);
  int64_t monotonic_ms = esp_timer_get_time() / 1000;
  return now >= 1577836800 ? (int64_t)now : monotonic_ms / 1000;
}

// 5x7 ASCII for compact status fields and a usable fallback when font16.bin is absent.
static const uint8_t FONT[59][5] = {
  {0x00,0x00,0x00,0x00,0x00},{0x00,0x00,0x5F,0x00,0x00},{0x00,0x07,0x00,0x07,0x00},
  {0x14,0x7F,0x14,0x7F,0x14},{0x24,0x2A,0x7F,0x2A,0x12},{0x23,0x13,0x08,0x64,0x62},
  {0x36,0x49,0x55,0x22,0x50},{0x00,0x05,0x03,0x00,0x00},{0x00,0x1C,0x22,0x41,0x00},
  {0x00,0x41,0x22,0x1C,0x00},{0x14,0x08,0x3E,0x08,0x14},{0x08,0x08,0x3E,0x08,0x08},
  {0x00,0x50,0x30,0x00,0x00},{0x08,0x08,0x08,0x08,0x08},{0x00,0x60,0x60,0x00,0x00},
  {0x20,0x10,0x08,0x04,0x02},{0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},
  {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4B,0x31},{0x18,0x14,0x12,0x7F,0x10},
  {0x27,0x45,0x45,0x45,0x39},{0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},
  {0x36,0x49,0x49,0x49,0x36},{0x06,0x49,0x49,0x29,0x1E},{0x00,0x36,0x36,0x00,0x00},
  {0x00,0x56,0x36,0x00,0x00},{0x08,0x14,0x22,0x41,0x00},{0x14,0x14,0x14,0x14,0x14},
  {0x00,0x41,0x22,0x14,0x08},{0x02,0x01,0x51,0x09,0x06},{0x32,0x49,0x79,0x41,0x3E},
  {0x7E,0x11,0x11,0x11,0x7E},{0x7F,0x49,0x49,0x49,0x36},{0x3E,0x41,0x41,0x41,0x22},
  {0x7F,0x41,0x41,0x22,0x1C},{0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},
  {0x3E,0x41,0x49,0x49,0x7A},{0x7F,0x08,0x08,0x08,0x7F},{0x00,0x41,0x7F,0x41,0x00},
  {0x20,0x40,0x41,0x3F,0x01},{0x7F,0x08,0x14,0x22,0x41},{0x7F,0x40,0x40,0x40,0x40},
  {0x7F,0x02,0x0C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},{0x3E,0x41,0x41,0x41,0x3E},
  {0x7F,0x09,0x09,0x09,0x06},{0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},
  {0x46,0x49,0x49,0x49,0x31},{0x01,0x01,0x7F,0x01,0x01},{0x3F,0x40,0x40,0x40,0x3F},
  {0x1F,0x20,0x40,0x20,0x1F},{0x3F,0x40,0x38,0x40,0x3F},{0x63,0x14,0x08,0x14,0x63},
  {0x07,0x08,0x70,0x08,0x07},{0x61,0x51,0x49,0x45,0x43},
};

void ui_fb_rect(int x, int y, int w, int h) {
  for (int py = y; py < y + h; py++) {
    if (py < 0 || py >= EPD_LANDSCAPE_H) continue;
    for (int px = x; px < x + w; px++) {
      if (px < 0 || px >= EPD_LANDSCAPE_W) continue;
      size_t index = py * EPD_FB_STRIDE + (px >> 3);
      s_fb[index] |= (uint8_t)(0x80U >> (px & 7));
    }
  }
}

static void white_rect(int x, int y, int w, int h) {
  for (int py = y; py < y + h; py++) {
    if (py < 0 || py >= EPD_LANDSCAPE_H) continue;
    for (int px = x; px < x + w; px++) {
      if (px < 0 || px >= EPD_LANDSCAPE_W) continue;
      size_t index = py * EPD_FB_STRIDE + (px >> 3);
      s_fb[index] &= (uint8_t)~(0x80U >> (px & 7));
    }
  }
}

static void ascii_char(int x, int y, int scale, char c) {
  if (c >= 'a' && c <= 'z') c -= 32;
  const uint8_t box[5] = {0x7F, 0x41, 0x41, 0x41, 0x7F};
  const uint8_t *glyph = (c >= 32 && c <= 90) ? FONT[c - 32] : box;
  for (int col = 0; col < 5; col++) {
    for (int row = 0; row < 7; row++) {
      if (glyph[col] & (1U << row)) {
        ui_fb_rect(x + col * scale, y + row * scale, scale, scale);
      }
    }
  }
}

static void ascii_text(int x, int y, int scale, const char *text) {
  for (; text && *text; text++, x += 6 * scale) ascii_char(x, y, scale, *text);
}

static int utext(int x, int y, int scale, const char *text) {
  if (ui_font_ready()) return ui_font_text(x, y, scale, text ? text : "");
  ascii_text(x, y + 4 * scale, scale, text ? text : "");
  return x + (int)strlen(text ? text : "") * 6 * scale;
}

static int utext_w(const char *text, int scale) {
  return ui_font_ready() ? ui_font_text_w(text ? text : "", scale)
                         : (int)strlen(text ? text : "") * 6 * scale;
}

static void ucenter(int y, int scale, const char *text) {
  utext((EPD_LANDSCAPE_W - utext_w(text, scale)) / 2, y, scale, text);
}

static const char *next_utf8(const char *text) {
  if (!text || !*text) return text;
  text++;
  while (*text && (((unsigned char)*text & 0xC0U) == 0x80U)) text++;
  return text;
}

static void wrap_text(int x, int y, int max_width, int max_lines,
                      int line_height, const char *text) {
  int cx = x;
  int line = 0;
  char one[8];
  while (text && *text && line < max_lines) {
    const char *next = next_utf8(text);
    size_t length = (size_t)(next - text);
    if (length >= sizeof(one)) break;
    memcpy(one, text, length);
    one[length] = '\0';
    int advance = utext_w(one, 1);
    if (cx + advance > x + max_width) {
      cx = x;
      if (++line >= max_lines) break;
    }
    utext(cx, y + line * line_height, 1, one);
    cx += advance;
    text = next;
  }
}

static bool load_page(const char *page_id) {
  memset(s_fb, 0, sizeof(s_fb));
  if (!s_assets_ready || !page_id) return false;
  char path[80];
  snprintf(path, sizeof(path), "/assets/ui154/%s.bin", page_id);
  FILE *file = fopen(path, "rb");
  if (!file) {
    ESP_LOGW(TAG, "missing UI asset: %s", path);
    return false;
  }
  size_t got = fread(s_fb, 1, sizeof(s_fb), file);
  int extra = fgetc(file);
  fclose(file);
  if (got != sizeof(s_fb) || extra != EOF) {
    ESP_LOGE(TAG, "invalid UI asset size: %s (%u)", path, (unsigned)got);
    memset(s_fb, 0, sizeof(s_fb));
    return false;
  }
  return true;
}

#define UI154_LAYOUT_MAX_LINES 8
#define UI154_LAYOUT_LINE_BYTES 256

typedef struct {
  char text[UI154_LAYOUT_LINE_BYTES];
  int width;
} layout_line_t;

static int layout_char_width(const char *start, size_t length, int pixel_size) {
  char one[8];
  if (!start || length == 0 || length >= sizeof(one)) return 0;
  memcpy(one, start, length);
  one[length] = '\0';
  return ui_font_ready() ? ui_font_text_w_px(one, pixel_size)
                         : (int)length * 6 * ((pixel_size + 6) / 7);
}

static bool layout_text_fits(const ui154_layout_field_t *field, const char *text) {
  int line = 1;
  int width = 0;
  while (text && *text) {
    if (*text == '\n') {
      if (++line > field->max_lines) return false;
      width = 0;
      text++;
      continue;
    }
    const char *next = next_utf8(text);
    int advance = layout_char_width(text, (size_t)(next - text), field->size);
    if (width > 0 && width + advance > field->width) {
      if (++line > field->max_lines) return false;
      width = 0;
    }
    width += advance;
    text = next;
  }
  return true;
}

static int layout_split_lines(const ui154_layout_field_t *field, const char *text,
                              bool latest, layout_line_t lines[UI154_LAYOUT_MAX_LINES]) {
  if (!text) text = "";
  if (latest) {
    while (*text && !layout_text_fits(field, text)) text = next_utf8(text);
  }
  int line = 0;
  size_t used = 0;
  memset(lines, 0, sizeof(layout_line_t) * UI154_LAYOUT_MAX_LINES);
  while (*text && line < field->max_lines && line < UI154_LAYOUT_MAX_LINES) {
    if (*text == '\n') {
      lines[line].text[used] = '\0';
      line++;
      used = 0;
      text++;
      continue;
    }
    const char *next = next_utf8(text);
    size_t length = (size_t)(next - text);
    int advance = layout_char_width(text, length, field->size);
    if (used > 0 && lines[line].width + advance > field->width) {
      lines[line].text[used] = '\0';
      line++;
      used = 0;
      if (line >= field->max_lines || line >= UI154_LAYOUT_MAX_LINES) break;
    }
    if (used + length + 1 >= UI154_LAYOUT_LINE_BYTES) break;
    memcpy(lines[line].text + used, text, length);
    used += length;
    lines[line].width += advance;
    text = next;
  }
  if (line < field->max_lines && line < UI154_LAYOUT_MAX_LINES) {
    lines[line].text[used] = '\0';
    line++;
  }
  return line;
}

static void layout_draw(const ui154_layout_field_t *field, const char *text,
                        bool clear, bool latest) {
  if (!field) return;
  if (clear) white_rect(field->x, field->y, field->width, field->height);
  layout_line_t lines[UI154_LAYOUT_MAX_LINES];
  int count = layout_split_lines(field, text, latest, lines);
  for (int line = 0; line < count; ++line) {
    int x = field->x;
    if (field->align == UI154_ALIGN_CENTER) x += (field->width - lines[line].width) / 2;
    else if (field->align == UI154_ALIGN_RIGHT) x += field->width - lines[line].width;
    if (ui_font_ready()) {
      ui_font_text_px(x, field->y + line * field->line_height, field->size, lines[line].text);
    } else {
      ascii_text(x, field->y + line * field->line_height,
                 field->size >= 14 ? 2 : 1, lines[line].text);
    }
  }
}

static void layout_draw_field(const char *page_id, const char *field_id,
                              const char *text) {
  layout_draw(ui154_layout_find(page_id, field_id), text, true, false);
}

static void layout_draw_field_latest(const char *page_id, const char *field_id,
                                     const char *text) {
  layout_draw(ui154_layout_find(page_id, field_id), text, true, true);
}

static bool load_layout_page(const char *page_id) {
  bool loaded = load_page(page_id);
  size_t count = 0;
  const ui154_layout_field_t *fields = ui154_layout_fields(&count);
  for (size_t i = 0; i < count; ++i) {
    if (strcmp(fields[i].page_id, page_id) == 0)
      layout_draw(&fields[i], fields[i].default_text, false, false);
  }
  return loaded;
}

static void draw_layout_battery(const char *page_id, const char *field_id,
                                uint8_t battery) {
  char value[8];
  snprintf(value, sizeof(value), "%u%%", (unsigned)battery);
  layout_draw_field(page_id, field_id, value);
}

static void fmt_mmss(char *output, size_t size, int64_t milliseconds) {
  if (milliseconds < 0) milliseconds = 0;
  if (milliseconds > 5999000) milliseconds = 5999000;
  unsigned seconds = (unsigned)(milliseconds / 1000);
  snprintf(output, size, "%02u:%02u", seconds / 60U, seconds % 60U);
}

static void account_localtime(time_t utc, struct tm *output) {
  int32_t offset_minutes = DEFAULT_UI_TIMEZONE_OFFSET_MINUTES;
  time_t shifted = utc + (time_t)offset_minutes * 60;
  gmtime_r(&shifted, output);
}

static bool agenda_before(const luoye_agenda_snapshot_t *agenda,
                          uint8_t lhs, uint8_t rhs) {
  const luoye_agenda_item_t *a = &agenda->items[lhs];
  const luoye_agenda_item_t *b = &agenda->items[rhs];
  if (a->has_time != b->has_time) return a->has_time;
  if (a->has_time && a->start_utc != b->start_utc)
    return a->start_utc < b->start_utc;
  return lhs < rhs;
}

static uint8_t agenda_collect_visible(const luoye_agenda_snapshot_t *agenda,
                                      int64_t now_utc,
                                      uint8_t *ordered, uint8_t capacity) {
  uint8_t count = 0;
  for (uint8_t i = 0; agenda && i < agenda->count && count < capacity; ++i) {
    const luoye_agenda_item_t *item = &agenda->items[i];
    if (item->dismissed || (item->has_time && item->start_utc < now_utc)) continue;
    uint8_t at = count;
    while (at > 0 && agenda_before(agenda, i, ordered[at - 1])) {
      ordered[at] = ordered[at - 1];
      --at;
    }
    ordered[at] = i;
    ++count;
  }
  return count;
}

static void screen_home(const app_state_t *state) {
  const char *page = "01_home";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785833364962", state->battery);
  luoye_agenda_snapshot_t agenda = {0};
  bool have_agenda = agenda_snapshot_get(&agenda);
  time_t now = time(NULL);
  struct tm local;
  account_localtime(now, &local);
  char clock[8] = "--:--";
  char date[32] = "时钟未同步";
  if (local.tm_year + 1900 >= 2020) {
    static const char *weekdays[] = {"周日","周一","周二","周三","周四","周五","周六"};
    snprintf(clock, sizeof(clock), "%02d:%02d", local.tm_hour, local.tm_min);
    snprintf(date, sizeof(date), "%d月%d日  %s", local.tm_mon + 1,
             local.tm_mday, weekdays[local.tm_wday]);
  }
  layout_draw_field(page, "time", clock);
  layout_draw_field(page, "date", date);

  uint8_t ordered[LUOYE_AGENDA_MAX_ITEMS];
  uint8_t visible = agenda_collect_visible(have_agenda ? &agenda : NULL,
                                           (int64_t)now, ordered,
                                           LUOYE_AGENDA_MAX_ITEMS);
  const char *next_time = "--:--";
  const char *next_content = have_agenda ? "今天没有后续日程" : "日程尚未同步";
  if (visible > 0) {
    const luoye_agenda_item_t *next = &agenda.items[ordered[0]];
    next_time = next->has_time ? next->display_time : "未定时间";
    next_content = next->title;
  }
  layout_draw_field(page, "next_label", next_time);
  layout_draw_field(page, "next_event", next_content);
}

static void screen_agenda(const app_state_t *state) {
  const char *page = "02_agenda";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785833322327", state->battery);
  time_t now = time(NULL);
  struct tm local;
  account_localtime(now, &local);
  char date[32];
  snprintf(date, sizeof(date), "今天 %d月%d日", local.tm_mon + 1, local.tm_mday);
  layout_draw_field(page, "date", date);

  luoye_agenda_snapshot_t agenda = {0};
  bool have = agenda_snapshot_get(&agenda);
  uint8_t ordered[LUOYE_AGENDA_MAX_ITEMS];
  uint8_t visible = agenda_collect_visible(have ? &agenda : NULL,
                                           (int64_t)now, ordered,
                                           LUOYE_AGENDA_MAX_ITEMS);
  uint8_t page_count = visible > 0 ? (uint8_t)((visible + 1U) / 2U) : 1U;
  uint8_t page_index = (uint8_t)(state->agenda_page % page_count);
  uint8_t first = (uint8_t)(page_index * 2U);
  const char *event_fields[] = {"event1", "event2"};
  for (uint8_t slot = 0; slot < 2; ++slot) {
    uint8_t position = (uint8_t)(first + slot);
    if (position < visible) {
      const luoye_agenda_item_t *item = &agenda.items[ordered[position]];
      char line[128];
      snprintf(line, sizeof(line), "%s\n%s",
               item->has_time && item->display_time[0]
                 ? item->display_time : "未定时间",
               item->title);
      layout_draw_field(page, event_fields[slot], line);
    } else {
      layout_draw_field(page, event_fields[slot],
                        slot == 0 && visible == 0
                          ? (have ? "暂无待办" : "日程尚未同步") : "");
    }
  }
  /* The approved background still contains the retired footer hint.  Clear
     the whole footer band before drawing the centered page number. */
  white_rect(4, 176, 125, 22);
  char pagination[12];
  snprintf(pagination, sizeof(pagination), "%u/%u",
           (unsigned)(page_index + 1U), (unsigned)page_count);
  layout_draw_field(page, "custom_1786374727793", pagination);
}

static void screen_device_status(const app_state_t *state) {
  const char *page = "03_device_status";
  /* Network and binding are owned by net_uploader.  The app-state copies are
     best-effort UI notifications and can briefly lag while pairing teardown
     posts several events.  Read the authoritative values when composing the
     status page so a healthy bound device cannot be painted as offline. */
  bool network_online = net_is_online();
  bool account_bound = net_is_bound();
  if (network_online != state->online) {
    ESP_LOGW(TAG,
             "LY|UI_STATUS|event=network_cache_mismatch cached=%d actual=%d corrected=1",
             state->online, network_online);
  }
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785833371686", state->battery);
  layout_draw_field(page, "network_value", network_online ? "已连接" : "离线");
  layout_draw_field(page, "cloud_value", account_bound ? "已绑定" : "未绑定");
  char backlog[16];
  snprintf(backlog, sizeof(backlog), "%u秒", (unsigned)state->backlog_s);
  layout_draw_field(page, "upload_value", backlog);
  layout_draw_field(page, "storage_value", state->sd_low ? "将满" : "可用");
  layout_draw_field(page, "overall", (network_online && !state->sd_low) ? "状态正常" : "需要检查");
  char battery[16];
  snprintf(battery, sizeof(battery), "电量 %u%%", (unsigned)state->battery);
  layout_draw_field(page, "battery", battery);
  char version[40];
  snprintf(version, sizeof(version), "固件 v%s", luoye_build_version());
  layout_draw_field(page, "version", version);
}

static void screen_charging(const app_state_t *state) {
  const char *page = "20_charging";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785833502795", state->battery);
  layout_draw_field(page, "title", state->charging == APP_CHG_FULL ? "已充满" : "充电中");
  layout_draw_field(page, "state", state->charging == APP_CHG_FULL ? "充电已完成" : "正在充电中");
}

static void screen_standby(const app_state_t *state) {
  if (state->charging != APP_CHG_NONE && state->page == 0) {
    screen_charging(state);
  } else if (state->page == 1) {
    screen_agenda(state);
  } else if (state->page == 2) {
    screen_device_status(state);
  } else {
    screen_home(state);
  }
}

static void screen_caption(const app_state_t *state) {
  luoye_live_result_t live = {0};
  const char *page = "05_meeting_caption";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832324207", state->battery);
  char elapsed[8];
  fmt_mmss(elapsed, sizeof(elapsed), app_state_elapsed_ms());
  char header[20];
  snprintf(header, sizeof(header), "录音 %s", elapsed);
  layout_draw_field(page, "header", header);
  bool have_live = net_live_snapshot(&live);
  const char *caption = NULL;
  if (have_live && live.kind == LUOYE_LIVE_MEETING && live.meeting_text[0]) {
    caption = live.meeting_text;
  } else {
    caption = "正在聆听，等待云端返回字幕。";
  }
  layout_draw_field_latest(page, "caption", caption);
  layout_draw_field(page, "footer", "录音键暂停 · 长按结束");
}

static void screen_record_status(const app_state_t *state) {
  const char *page = "06_meeting_status";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832327785", state->battery);
  char elapsed[8];
  fmt_mmss(elapsed, sizeof(elapsed), app_state_elapsed_ms());
  layout_draw_field(page, "duration", elapsed);
  layout_draw_field(page, "local_value", "写入正常");
  layout_draw_field(page, "cloud_value", state->cloud_online ? "已接收" : "待连接");
  char backlog[16];
  snprintf(backlog, sizeof(backlog), "%u秒", (unsigned)state->backlog_s);
  layout_draw_field(page, "backlog_value", backlog);
  char battery[8];
  snprintf(battery, sizeof(battery), "%u%%", (unsigned)state->battery);
  layout_draw_field(page, "battery_value", battery);
  layout_draw_field(page, "storage_value", state->sd_low ? "将满" : "正常");
}

static void screen_recording(const app_state_t *state) {
  if (state->locked) {
    const char *page = "08_meeting_locked";
    load_layout_page(page);
    draw_layout_battery(page, "battery", state->battery);
    char elapsed[8];
    fmt_mmss(elapsed, sizeof(elapsed), app_state_elapsed_ms());
    layout_draw_field(page, "duration", elapsed);
    layout_draw_field(page, "unlock", "长按设置键解锁");
    return;
  }
  if (state->paused) {
    const char *page = "07_meeting_paused";
    load_layout_page(page);
    draw_layout_battery(page, "battery_1785832331742", state->battery);
    char elapsed[8];
    fmt_mmss(elapsed, sizeof(elapsed), app_state_elapsed_ms());
    layout_draw_field(page, "duration", elapsed);
    layout_draw_field(page, "footer", "录音键继续");
    return;
  }
  if ((state->page & 1U) != 0) {
    screen_record_status(state);
    layout_draw_field("06_meeting_status", "footer", "设置键 返回字幕");
  } else {
    screen_caption(state);
  }
}

static void screen_todo_listening(const app_state_t *state) {
  const char *page = "10_todo_listening";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832365783", state->battery);
  char elapsed[8];
  fmt_mmss(elapsed, sizeof(elapsed), app_state_todo_elapsed_ms());
  char timer[24];
  snprintf(timer, sizeof(timer), "%s / 00:30", elapsed);
  layout_draw_field(page, "timer", timer);
}

static void screen_todo_confirm(const app_state_t *state) {
  const char *page = "11_todo_confirm";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832367364", state->battery);
  luoye_todo_item_t todo = {0};
  bool have = todo_latest(&todo);
  layout_draw_field(page, "time", have && todo.display_time[0] ? todo.display_time : "时间待确认");
  layout_draw_field(page, "todo", have && todo.title[0] ? todo.title : "请确认识别结果");
  layout_draw_field(page, "footer", "待办确认 · 设置取消");
}

static void screen_todo_created(const app_state_t *state) {
  const char *page = "12_todo_created";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832368724", state->battery);
  luoye_todo_item_t todo = {0};
  bool have = todo_latest(&todo);
  layout_draw_field(page, "time", have && todo.display_time[0] ? todo.display_time : "时间待确认");
  layout_draw_field(page, "todo", have && todo.title[0] ? todo.title : "已加入待办列表");
}

static void screen_todo_failed(const app_state_t *state) {
  const char *page = "22_storage_error";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832389733", state->battery);
  layout_draw_field(page, "title", "待办处理失败");
  layout_draw_field(page, "footer", "设置键 返回主页");
}

static void screen_reminder(const app_state_t *state) {
  const char *page = "13_schedule_reminder";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832370420", state->battery);
  const char *title = state->reminder[0] ? state->reminder : "日程提醒";
  layout_draw_field(page, "agenda", title);
  luoye_agenda_snapshot_t agenda = {0};
  const char *display_time = "--:--";
  if (agenda_snapshot_get(&agenda)) {
    for (uint8_t i = 0; i < agenda.count; ++i) {
      if (strcmp(agenda.items[i].title, title) == 0 && agenda.items[i].display_time[0]) {
        display_time = agenda.items[i].display_time;
        break;
      }
    }
  }
  layout_draw_field(page, "time", display_time);
  layout_draw_field(page, "footer2", "长按设置键推迟10分钟");
}

static void centered_message(const char *title, const char *line1, const char *line2) {
  memset(s_fb, 0, sizeof(s_fb));
  if (title) ucenter(34, 1, title);
  if (line1) ucenter(75, 2, line1);
  if (line2) wrap_text(12, 126, 176, 2, 18, line2);
}

static void screen_sync(const app_state_t *state) {
  const char *page = "16_wifi_connect";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832374716", state->battery);
  if (state->overlay == APP_OV_SYNC_CONFIRM) {
    layout_draw_field(page, "title", "同步录音");
    layout_draw_field(page, "subtitle", "按待办键检查并继续");
    layout_draw_field(page, "footer", "设置键 取消");
  } else if (state->overlay == APP_OV_SYNC_PROGRESS) {
    char progress[48];
    if (state->cloud_online) {
      if (state->backlog_s) {
        snprintf(progress, sizeof(progress), "剩余约%u秒",
                 (unsigned)state->backlog_s);
      } else {
        snprintf(progress, sizeof(progress), "正在确认服务器状态");
      }
    } else {
      snprintf(progress, sizeof(progress), "等待网络连接");
    }
    layout_draw_field(page, "title", "正在同步");
    layout_draw_field(page, "subtitle", progress);
    layout_draw_field(page, "footer", "设置键 返回主页");
  } else if (state->overlay == APP_OV_SYNC_DONE) {
    layout_draw_field(page, "title", "同步完成");
    layout_draw_field(page, "subtitle", "本地录音已清理");
    layout_draw_field(page, "footer", "即将返回主页");
  } else {
    layout_draw_field(page, "title", "同步不可用");
    layout_draw_field(page, "subtitle", "请先联网并绑定账号");
    layout_draw_field(page, "footer", "设置键 返回主页");
  }
}

static void screen_pairing(const app_state_t *state) {
  net_pairing_info_t pair = {0};
  net_get_pairing_info(&pair);
  switch (state->pairing) {
    case APP_PAIR_AP_READY:
      load_layout_page("15_pair_hotspot");
      draw_layout_battery("15_pair_hotspot", "battery_1785832373333", state->battery);
      layout_draw_field("15_pair_hotspot", "ssid", pair.ap_ssid[0] ? pair.ap_ssid : "LUOYE-XXXX");
      layout_draw_field("15_pair_hotspot", "password", "无需密码");
      layout_draw_field("15_pair_hotspot", "address", "192.168.4.1");
      break;
    case APP_PAIR_WIFI_CONNECTING:
      load_layout_page("16_wifi_connect");
      draw_layout_battery("16_wifi_connect", "battery_1785832374716", state->battery);
      layout_draw_field("16_wifi_connect", "footer", "设置键 取消");
      break;
    case APP_PAIR_WIFI_CONNECTED:
      load_layout_page("17_network_ok");
      draw_layout_battery("17_network_ok", "battery_1785832376390", state->battery);
      layout_draw_field("17_network_ok", "footer", "设置键 返回");
      break;
    case APP_PAIR_CLAIM_PENDING:
      load_layout_page("18_bind_code");
      draw_layout_battery("18_bind_code", "battery_1785832378105", state->battery);
      layout_draw_field("18_bind_code", "code", pair.pairing_code[0] ? pair.pairing_code : "--- ---");
      layout_draw_field("18_bind_code", "footer", "设置键 取消");
      break;
    case APP_PAIR_BOUND:
      load_layout_page("19_bind_ok");
      draw_layout_battery("19_bind_ok", "battery_1785832379655", state->battery);
      char account[64];
      snprintf(account, sizeof(account), "已绑定 %s",
               pair.masked_account[0] ? pair.masked_account : "账号");
      layout_draw_field("19_bind_ok", "account", account);
      layout_draw_field("19_bind_ok", "footer", "设置键 返回主页");
      break;
    case APP_PAIR_ERROR:
      load_layout_page("16_wifi_connect");
      draw_layout_battery("16_wifi_connect", "battery_1785832374716", state->battery);
      layout_draw_field("16_wifi_connect", "title", state->online ? "服务器不可用" : "网络连接失败");
      layout_draw_field("16_wifi_connect", "subtitle", "按设置键返回");
      layout_draw_field("16_wifi_connect", "footer", "设置键 返回主页");
      break;
    default:
      centered_message("连接落叶", "正在启动", "请稍候");
      break;
  }
}

static void screen_storage_error(const app_state_t *state) {
  if (state->error == APP_ERR_LOW_BATTERY) {
    load_layout_page("21_low_battery");
    draw_layout_battery("21_low_battery", "battery_1785832385388", state->battery);
    layout_draw_field("21_low_battery", "footer", "设置键 返回主页");
    return;
  }
  const char *page = "22_storage_error";
  load_layout_page(page);
  draw_layout_battery(page, "battery_1785832389733", state->battery);
  layout_draw_field(page, "footer", "设置键 返回主页");
  if (state->error == APP_ERR_MIC) {
    layout_draw_field(page, "title", "麦克风异常");
  } else if (state->error == APP_ERR_SD_FULL) {
    layout_draw_field(page, "title", "存储空间已满");
    layout_draw_field(page, "footer", "录音未保存");
  }
}

static void build_screen(const app_state_t *state) {
  if (state->mode == APP_MODE_OFF) {
    memset(s_fb, 0, sizeof(s_fb));            // physical power-off request leaves a white panel
    return;
  }
  switch (state->overlay) {
    case APP_OV_REMINDER:     screen_reminder(state); return;
    case APP_OV_TODO_LISTEN:  screen_todo_listening(state); return;
    case APP_OV_TODO_CONFIRM: screen_todo_confirm(state); return;
    case APP_OV_TODO_CREATED:
    case APP_OV_TODO_OK:      screen_todo_created(state); return;
    case APP_OV_TODO_FAILED:  screen_todo_failed(state); return;
    case APP_OV_SNOOZED:
      load_layout_page("14_reminder_alt");
      draw_layout_battery("14_reminder_alt", "battery_1785832371728", state->battery);
      return;
    case APP_OV_LOCKED_HINT:
      load_layout_page("08_meeting_locked");
      draw_layout_battery("08_meeting_locked", "battery", state->battery);
      layout_draw_field("08_meeting_locked", "unlock", "长按设置键解锁");
      return;
    case APP_OV_POWER_CONFIRM:
      centered_message("电源", "确认关机", "录音键确认  设置键取消");
      return;
    case APP_OV_SYNC_CONFIRM:
    case APP_OV_SYNC_PROGRESS:
    case APP_OV_SYNC_DONE:
    case APP_OV_SYNC_FAILED:
      screen_sync(state);
      return;
    default: break;
  }

  switch (state->mode) {
    case APP_MODE_STANDBY:
      screen_standby(state);
      break;
    case APP_MODE_STARTING:
      load_layout_page("04_meeting_prepare");
      draw_layout_battery("04_meeting_prepare", "battery_1785832320453", state->battery);
      break;
    case APP_MODE_RECORDING:
      screen_recording(state);
      break;
    case APP_MODE_CLOSING: {
      const char *page = "09_meeting_saving";
      load_layout_page(page);
      draw_layout_battery(page, "battery_1785832363998", state->battery);
      char elapsed[8];
      fmt_mmss(elapsed, sizeof(elapsed), state->ended_elapsed_ms);
      char saved[24];
      snprintf(saved, sizeof(saved), "已录 %s", elapsed);
      layout_draw_field(page, "duration", saved);
      break;
    }
    case APP_MODE_ENDING: {
      load_layout_page("09_meeting_saving");
      draw_layout_battery("09_meeting_saving", "battery_1785832363998", state->battery);
      char elapsed[8];
      fmt_mmss(elapsed, sizeof(elapsed), state->ended_elapsed_ms);
      char saved[24];
      snprintf(saved, sizeof(saved), "已录 %s", elapsed);
      layout_draw_field("09_meeting_saving", "duration", saved);
      layout_draw_field("09_meeting_saving", "title", "已保存");
      layout_draw_field("09_meeting_saving", "subtitle",
                        state->cloud_online ? "云端处理中" : "联网后自动补传");
      break;
    }
    case APP_MODE_STORAGE_ERROR:
      screen_storage_error(state);
      break;
    case APP_MODE_PAIRING:
      screen_pairing(state);
      break;
    default:
      screen_home(state);
      break;
  }
}

static esp_err_t render_once_panel(app_render_t kind) {
  static bool stack_logged;
  s_busy = true;
  app_state_t snapshot = *app_state_get();
  uint32_t next_screen = screen_identity(&snapshot);
  app_render_t requested_kind = kind;
  if (s_have_panel_screen && next_screen != s_panel_screen &&
      kind < APP_RENDER_FAST) {
    kind = APP_RENDER_FAST;
    ESP_LOGI(TAG,
             "LY|UI|event=page_transition_upgrade from=0x%08lx to=0x%08lx requested=%d applied=fast",
             (unsigned long)s_panel_screen, (unsigned long)next_screen,
             (int)requested_kind);
  }
  build_screen(&snapshot);
  if (kind == APP_RENDER_FULL) {
    epd_frame_full(s_fb);
  } else if (kind == APP_RENDER_CLOCK_PARTIAL) {
    /* Rebuild the current clock and battery together, then always send the
       same panel-safe window.  This avoids unreliable tiny glyph rectangles
       after light-sleep wake while keeping Wi-Fi and the rest of the panel
       asleep. */
    epd_frame_partial_window(s_fb, HOME_CLOCK_BATTERY_X,
                             HOME_CLOCK_BATTERY_Y,
                             HOME_CLOCK_BATTERY_WIDTH,
                             HOME_CLOCK_BATTERY_HEIGHT);
  } else if (kind == APP_RENDER_STATUS_PARTIAL) {
    /* Network, binding, backlog, storage and battery can be far apart. Let the
       panel driver bound the update to the pixels that actually changed. */
    epd_frame_partial_auto(s_fb);
  } else if (kind == APP_RENDER_PARTIAL) {
    // Only the dynamic recording header/body is touched. The approved border
    // and fixed footer remain electrically quiet until the periodic FAST refresh.
    epd_frame_partial_window(s_fb, 4, 8, 192, 171);
  } else {
    epd_frame_fast(s_fb);
  }
  esp_err_t panel_error = epd_last_error();
  if (panel_error == ESP_OK) {
    s_panel_screen = next_screen;
    s_have_panel_screen = true;
  }
  epd_deep_sleep();
  if (!stack_logged) {
    stack_logged = true;
    ESP_LOGI(TAG, "LY|STACK|task=ui free=%u unit_bytes=%u",
             (unsigned)uxTaskGetStackHighWaterMark(NULL),
             (unsigned)sizeof(StackType_t));
  }
  s_busy = false;
  return panel_error;
}

static esp_err_t render_once(app_render_t kind) {
  return render_once_panel(kind);
}

static void ui_task(void *argument) {
  (void)argument;
  int64_t displayed_five_second = -1;
  int64_t displayed_home_minute = -1;
  for (;;) {
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
    int kind;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    kind = s_pending;
    s_pending = -1;
    xSemaphoreGive(s_lock);
    if (kind >= 0) {
      esp_err_t panel_error = render_once((app_render_t)kind);
      const app_state_t *rendered = app_state_get();
      if (rendered->mode == APP_MODE_RECORDING && !rendered->paused &&
          !rendered->locked && rendered->overlay == APP_OV_NONE &&
          (rendered->page & 1U) == 0) {
        time_t now = time(NULL);
        int64_t monotonic_ms = esp_timer_get_time() / 1000;
        int64_t seconds = now >= 1577836800 ? (int64_t)now
                                            : monotonic_ms / 1000;
        displayed_five_second = seconds / 5;
      } else {
        displayed_five_second = -1;
      }
      if (!home_clock_active(rendered)) {
        displayed_home_minute = -1;
      } else if (panel_error == ESP_OK) {
        displayed_home_minute = ui_clock_seconds() / 60;
      } else {
        /* Do not acknowledge a minute that was not accepted by the panel.
           The one-second UI loop retries without waking networking. */
        displayed_home_minute = -1;
        ESP_LOGW(TAG,
                 "LY|UI_CLOCK|event=render_retry reason=%s minute=%lld",
                 esp_err_to_name(panel_error),
                 (long long)(ui_clock_seconds() / 60));
      }
      continue;
    }
    const app_state_t *state = app_state_get();
    int64_t now_ms = esp_timer_get_time() / 1000;
    if (state->mode == APP_MODE_RECORDING && !state->paused && !state->locked &&
        state->overlay == APP_OV_NONE && (state->page & 1U) == 0) {
      time_t now = time(NULL);
      int64_t seconds = now >= 1577836800 ? (int64_t)now : now_ms / 1000;
      int64_t five_second = seconds / 5;
      if (displayed_five_second < 0) {
        displayed_five_second = five_second;
      } else if (five_second != displayed_five_second) {
        /* Bound recording-page partial accumulation to five minutes.  FAST
           repaints the whole panel without the slower black/white FULL cycle. */
        bool periodic_fast = (five_second % 60) == 0;
        render_once_panel(periodic_fast ? APP_RENDER_FAST : APP_RENDER_PARTIAL);
        displayed_five_second = five_second;
        ESP_LOGI(TAG,
                 "LY|UI_RECORD|refresh=%s second=%u view=caption",
                 periodic_fast ? "fast" : "partial",
                 (unsigned)(seconds % 60));
      }
    } else {
      displayed_five_second = -1;
    }
    if (home_clock_active(state)) {
      int64_t seconds = ui_clock_seconds();
      int64_t minute = seconds / 60;
      if (displayed_home_minute < 0 || minute != displayed_home_minute) {
        time_t now = time(NULL);
        struct tm local = {0};
        bool top_of_hour = false;
        bool ten_minute = false;
        if (now >= 1577836800) {
          account_localtime(now, &local);
          top_of_hour = local.tm_min == 0;
          ten_minute = (local.tm_min % 10) == 0;
        }
        app_render_t clock_render = top_of_hour ? APP_RENDER_FULL :
                                    ten_minute ? APP_RENDER_FAST :
                                                 APP_RENDER_CLOCK_PARTIAL;
        esp_err_t panel_error = render_once_panel(clock_render);
        if (panel_error == ESP_OK) displayed_home_minute = minute;
        ESP_LOGI(TAG, "LY|UI_CLOCK|refresh=%s minute=%lld result=%s",
                 top_of_hour ? "full" :
                 ten_minute ? "fast" : "clock+battery-partial",
                 (long long)minute, esp_err_to_name(panel_error));
      }
    } else {
      displayed_home_minute = -1;
    }
  }
}

esp_err_t ui_init(void) {
  s_lock = xSemaphoreCreateMutex();
  if (!s_lock) return ESP_ERR_NO_MEM;
  esp_err_t err = epd_init();
  if (err != ESP_OK) return err;
  epd_power(true);
  esp_err_t font_error = ui_font_init();
  FILE *probe = fopen("/assets/ui154/01_home.bin", "rb");
  s_assets_ready = probe != NULL;
  if (probe) fclose(probe);
  if (!s_assets_ready) ESP_LOGE(TAG, "approved 1.54-inch UI assets are missing");
  /* Timeline text adds a bounded local composition buffer. */
  if (xTaskCreate(ui_task, "ui", 12288, NULL, 6, &s_task) != pdPASS) return ESP_ERR_NO_MEM;
  ESP_LOGI(TAG, "UI ready: GDEY0154D67 200x200 BW-FAST, assets=%s font=%s (%s) layout=%.12s",
           s_assets_ready ? "OK" : "MISSING", ui_font_ready() ? "16px" : "fallback",
           esp_err_to_name(font_error), ui154_layout_sha256());
  return s_assets_ready ? ESP_OK : ESP_ERR_NOT_FOUND;
}

void ui_request_render(app_render_t kind) {
  if (!s_lock) return;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  if ((int)kind > s_pending) s_pending = kind;
  xSemaphoreGive(s_lock);
  if (s_task) xTaskNotifyGive(s_task);
}

bool ui_wait_idle(uint32_t timeout_ms) {
  if (!s_task) return false;
  int64_t deadline = esp_timer_get_time() / 1000 + timeout_ms;
  while (esp_timer_get_time() / 1000 < deadline) {
    if (!s_busy && s_pending < 0) return true;
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  return false;
}
