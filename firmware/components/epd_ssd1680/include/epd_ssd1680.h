// epd_ssd1680.h — GDEY0213B74 (SSD1680, 2.13" 250×122) 驱动
// 时序移植自屏厂官方包 A32-GDEY0213B74-2FP-20230616(Arduino 版),命令逐条对应。
// 上层统一用「横屏帧缓冲」:250 宽 × 122 高,1bpp,行优先,bit7=最左像素,1=黑。
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#define EPD_LANDSCAPE_W  250
#define EPD_LANDSCAPE_H  122
#define EPD_FB_BYTES     (((EPD_LANDSCAPE_W + 7) / 8) * EPD_LANDSCAPE_H)   // 32×122 = 3904

// 面板原生竖屏 RAM:128(x, 其中可见 122)× 250(y)
#define EPD_PANEL_W      128
#define EPD_PANEL_H      250
#define EPD_PANEL_BYTES  (EPD_PANEL_W / 8 * EPD_PANEL_H)                   // 4000

// 首次点屏若发现镜像/颠倒,改这三个宏即可,不用动映射代码
// 新板实测校准：翻转面板短轴，并保持横屏长轴字序，避免文字左右镜像。
// 最终阅读方向与面板坐标箭头一致。
#define EPD_ROT_FLIP_X   1
#define EPD_ROT_FLIP_Y   0
#define EPD_X_OFFSET     0     // 122 可见列在 128 RAM 中的起始偏移(B74 通常为 0)

esp_err_t epd_init(void);            // SPI 总线 + GPIO;不上电、不刷屏
esp_err_t epd_last_error(void);      // 最近一次刷新中的 SPI 传输错误
void epd_power(bool on);             // TPS22918 通断 VCC_EPD(开后等 10ms 再操作)
void epd_frame_full(const uint8_t *fb);      // 全刷(~2s,去残影)  = 厂商 EPD_Update 0xF7
void epd_frame_fast(const uint8_t *fb);      // 快刷(~1s)          = EPD_Update_Fast 0xC7
void epd_base_map(const uint8_t *fb);        // 局刷底图(0x24+0x26),局刷序列的第一帧必须调它
void epd_frame_partial(const uint8_t *fb);   // 整屏局刷(无闪烁)   = EPD_Part_Update 0xFF
void epd_deep_sleep(void);           // 0x10 深睡;下次刷新前驱动会自动硬复位唤醒
