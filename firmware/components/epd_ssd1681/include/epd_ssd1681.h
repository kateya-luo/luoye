// epd_ssd1681.h — GDEY0154D67 (SSD1681, 1.54" 200x200) driver.
// UI framebuffer: 200x200, row-major, bit7 is the left-most pixel, 1=black.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#define EPD_LANDSCAPE_W  200
#define EPD_LANDSCAPE_H  200
#define EPD_FB_STRIDE     ((EPD_LANDSCAPE_W + 7) / 8)
#define EPD_FB_BYTES      (EPD_FB_STRIDE * EPD_LANDSCAPE_H)

// Square-panel orientation. Change only these macros after a physical first-frame
// check; the renderer and UI assets always stay in normal top-left coordinates.
#define EPD_SWAP_XY       1
#define EPD_FLIP_X        1
#define EPD_FLIP_Y        1

esp_err_t epd_init(void);
esp_err_t epd_last_error(void);
void epd_power(bool on);
void epd_frame_full(const uint8_t *fb);
void epd_frame_fast(const uint8_t *fb);
// Update one logical framebuffer rectangle with the vendor 0xFF partial
// waveform. Coordinates use the renderer's normal top-left 200x200 space.
void epd_frame_partial_window(const uint8_t *fb, uint16_t x, uint16_t y,
                              uint16_t width, uint16_t height);
// Update the smallest byte-aligned panel rectangle that differs from the last
// displayed frame. Intended for several independent fields on one visual page.
void epd_frame_partial_auto(const uint8_t *fb);
void epd_deep_sleep(void);
