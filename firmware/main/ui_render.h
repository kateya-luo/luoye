// ui_render.h — state snapshot -> approved 200x200 UI -> SSD1681.
// Clock/status fields use SSD1681 partial windows. Every visual page transition
// uses FAST. V2.2 recording uses one page: a latest-suffix one-line ticker over
// two timeline summary rows updated only by the minute FAST refresh; protocol
// final/partial state remains invisible.
#pragma once
#include "app_state.h"
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

esp_err_t ui_init(void);                       // epd 上电 + 字库加载 + 渲染任务
void ui_request_render(app_render_t kind);     // 异步,可合并(取最高档)
bool ui_wait_idle(uint32_t timeout_ms);        // 关机前等最后一帧画完
void ui_fb_rect(int x, int y, int w, int h);   // 向当前帧缓冲画黑块(ui_font 用,仅渲染期内有效)
