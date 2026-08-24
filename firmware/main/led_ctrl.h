// led_ctrl.h — 三 LED 状态灯(语义与模拟器 renderLeds() 一致)
#pragma once
#include <stdbool.h>
#include "esp_err.h"

esp_err_t led_ctrl_init(void);
void led_ctrl_mark_flash(void);   // REC 红灯双闪 760ms(标记重点/收藏)
void led_ctrl_self_test(void);    // 开机三灯轮流自检(约 1.4s,阻塞调用方之外的灯任务)
