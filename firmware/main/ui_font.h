#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t ui_font_init(void);
bool ui_font_ready(void);
int ui_font_text(int x, int y, int scale, const char *utf8);
int ui_font_text_w(const char *utf8, int scale);
int ui_font_text_px(int x, int y, int pixel_size, const char *utf8);
int ui_font_text_w_px(const char *utf8, int pixel_size);
