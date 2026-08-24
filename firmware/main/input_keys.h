// input_keys.h — 三键输入:去抖 + 短按/长按/按住(阈值随状态机当前态变化)
#pragma once
#include "app_state.h"
#include "esp_err.h"

// 事件通过 app_post 回调送入 app 事件循环(在 app_main.c 定义)
typedef void (*key_post_fn)(app_event_t ev, int32_t arg);

esp_err_t input_keys_init(key_post_fn post);
