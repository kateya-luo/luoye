// Exact-size monochrome font renderer for the 200x200 e-paper UI.
#include "ui_font.h"
#include "ui_render.h"

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_spiffs.h"

typedef struct {
  uint8_t *blob;
  const uint16_t *codepoints;
  const uint8_t *glyphs;
  uint32_t count;
  uint16_t cell;
  uint16_t row_bytes;
  uint32_t glyph_bytes;
  size_t file_bytes;
} font_blob_t;

static const char *TAG = "font";
static const uint8_t s_required_sizes[] = {
  8, 9, 10, 11, 12, 13, 14, 16, 18, 20, 22, 24, 26, 32, 37, 40, 51,
};
static font_blob_t s_strikes[sizeof(s_required_sizes)];
static size_t s_loaded;

static esp_err_t load_font(const char *path, font_blob_t *font) {
  FILE *file = fopen(path, "rb");
  if (!file) return ESP_ERR_NOT_FOUND;
  fseek(file, 0, SEEK_END);
  long length = ftell(file);
  rewind(file);
  if (length < 16) {
    fclose(file);
    return ESP_ERR_INVALID_SIZE;
  }
  uint8_t *blob = heap_caps_malloc((size_t)length, MALLOC_CAP_SPIRAM);
  if (!blob) {
    fclose(file);
    return ESP_ERR_NO_MEM;
  }
  if (fread(blob, 1, (size_t)length, file) != (size_t)length) {
    fclose(file);
    heap_caps_free(blob);
    return ESP_FAIL;
  }
  fclose(file);
  if (memcmp(blob, "CMF1", 4) != 0) {
    heap_caps_free(blob);
    return ESP_ERR_INVALID_RESPONSE;
  }

  uint16_t cell = 0;
  uint16_t row_bytes = 0;
  uint32_t count = 0;
  memcpy(&cell, blob + 4, sizeof(cell));
  memcpy(&row_bytes, blob + 6, sizeof(row_bytes));
  memcpy(&count, blob + 8, sizeof(count));
  if (cell < 8 || cell > 64 || count == 0) {
    heap_caps_free(blob);
    return ESP_ERR_INVALID_SIZE;
  }
  if (row_bytes == 0) row_bytes = (uint16_t)((cell + 7U) / 8U);
  uint32_t glyph_bytes = (uint32_t)cell * row_bytes;
  size_t glyph_offset = 16U + (size_t)count * sizeof(uint16_t);
  size_t required = glyph_offset + (size_t)count * glyph_bytes;
  if (required > (size_t)length) {
    heap_caps_free(blob);
    return ESP_ERR_INVALID_SIZE;
  }

  *font = (font_blob_t){
    .blob = blob,
    .codepoints = (const uint16_t *)(blob + 16),
    .glyphs = blob + glyph_offset,
    .count = count,
    .cell = cell,
    .row_bytes = row_bytes,
    .glyph_bytes = glyph_bytes,
    .file_bytes = (size_t)length,
  };
  return ESP_OK;
}

bool ui_font_ready(void) {
  return s_loaded == sizeof(s_required_sizes) / sizeof(s_required_sizes[0]);
}

esp_err_t ui_font_init(void) {
  esp_vfs_spiffs_conf_t config = {
    .base_path = "/assets",
    .partition_label = "assets",
    .max_files = 3,
    .format_if_mount_failed = false,
  };
  esp_err_t err = esp_vfs_spiffs_register(&config);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "assets mount failed: %s", esp_err_to_name(err));
    return err;
  }

  size_t total_bytes = 0;
  s_loaded = 0;
  for (size_t i = 0; i < sizeof(s_required_sizes); ++i) {
    char path[32];
    if (s_required_sizes[i] == 16) {
      snprintf(path, sizeof(path), "/assets/font16.bin");
    } else {
      snprintf(path, sizeof(path), "/assets/font_%02u.bin",
               (unsigned)s_required_sizes[i]);
    }
    err = load_font(path, &s_strikes[i]);
    if (err != ESP_OK || s_strikes[i].cell != s_required_sizes[i]) {
      ESP_LOGE(TAG, "native strike %upx unavailable: %s",
               (unsigned)s_required_sizes[i], esp_err_to_name(err));
      return err == ESP_OK ? ESP_ERR_INVALID_SIZE : err;
    }
    total_bytes += s_strikes[i].file_bytes;
    ++s_loaded;
  }
  ESP_LOGI(TAG, "LY|FONT|family=SimSun mode=mono-native antialias=off strikes=%u bytes=%u scaling=off",
           (unsigned)s_loaded, (unsigned)total_bytes);
  return ESP_OK;
}

static uint32_t next_codepoint(const char **cursor) {
  const uint8_t *text = (const uint8_t *)*cursor;
  uint32_t cp;
  int length;
  if (text[0] < 0x80) {
    cp = text[0];
    length = 1;
  } else if ((text[0] & 0xE0) == 0xC0) {
    cp = text[0] & 0x1F;
    length = 2;
  } else if ((text[0] & 0xF0) == 0xE0) {
    cp = text[0] & 0x0F;
    length = 3;
  } else {
    (*cursor)++;
    return 0xFFFD;
  }
  for (int i = 1; i < length; ++i) {
    if ((text[i] & 0xC0) != 0x80) {
      (*cursor)++;
      return 0xFFFD;
    }
    cp = (cp << 6) | (text[i] & 0x3F);
  }
  *cursor += length;
  return cp;
}

static const font_blob_t *find_strike(int pixel_size) {
  for (size_t i = 0; i < s_loaded; ++i) {
    if (s_strikes[i].cell == pixel_size) return &s_strikes[i];
  }
  return NULL;
}

static const uint8_t *find_glyph(const font_blob_t *font, uint32_t cp) {
  uint32_t low = 0;
  uint32_t high = font->count;
  while (low < high) {
    uint32_t middle = (low + high) / 2;
    if (font->codepoints[middle] < cp) low = middle + 1;
    else high = middle;
  }
  if (low >= font->count || font->codepoints[low] != cp) return NULL;
  return font->glyphs + (size_t)low * font->glyph_bytes;
}

static void draw_native_glyph(int x, int y, const font_blob_t *font,
                              const uint8_t *glyph) {
  for (int row = 0; row < font->cell; ++row) {
    for (int col = 0; col < font->cell; ++col) {
      if (glyph[row * font->row_bytes + (col >> 3)] & (0x80U >> (col & 7))) {
        ui_fb_rect(x + col, y + row, 1, 1);
      }
    }
  }
}

static int glyph_advance(uint32_t cp, int pixel_size) {
  return cp < 0x100 ? (pixel_size + 1) / 2 : pixel_size;
}

int ui_font_text_px(int x, int y, int pixel_size, const char *utf8) {
  if (pixel_size < 1) return x;
  const font_blob_t *font = find_strike(pixel_size);
  const char *cursor = utf8 ? utf8 : "";
  while (*cursor) {
    uint32_t cp = next_codepoint(&cursor);
    const uint8_t *glyph = font ? find_glyph(font, cp) : NULL;
    if (glyph) {
      draw_native_glyph(x, y, font, glyph);
    } else if (cp != ' ') {
      int side = pixel_size > 3 ? pixel_size - 2 : pixel_size;
      ui_fb_rect(x + 1, y + 1, side, 1);
      ui_fb_rect(x + 1, y + pixel_size - 2, side, 1);
      ui_fb_rect(x + 1, y + 1, 1, side);
      ui_fb_rect(x + pixel_size - 2, y + 1, 1, side);
    }
    x += glyph_advance(cp, pixel_size);
  }
  return x;
}

int ui_font_text_w_px(const char *utf8, int pixel_size) {
  const char *cursor = utf8 ? utf8 : "";
  int width = 0;
  while (*cursor) width += glyph_advance(next_codepoint(&cursor), pixel_size);
  return width;
}

int ui_font_text(int x, int y, int scale, const char *utf8) {
  return ui_font_text_px(x, y, 16 * scale, utf8);
}

int ui_font_text_w(const char *utf8, int scale) {
  return ui_font_text_w_px(utf8, 16 * scale);
}
