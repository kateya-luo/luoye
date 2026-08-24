// epd_ssd1680.c — 命令序列 1:1 移植厂商 Display_EPD_W21.cpp,注释标注原函数名。
#include "epd_ssd1680.h"
#include "board_pins.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_log.h"

static const char *TAG = "epd";
static spi_device_handle_t s_spi;
static uint8_t s_panel_buf[EPD_PANEL_BYTES];   // 旋转后的面板缓冲
static bool s_powered, s_sleeping;
static esp_err_t s_last_error;

// ---------- 底层 ----------
static void tx(const void *buf, size_t len) {
  if (s_last_error != ESP_OK || !s_spi || len == 0) return;
  spi_transaction_t t = {.length = len * 8, .tx_buffer = buf};
  esp_err_t err = spi_device_polling_transmit(s_spi, &t);
  if (err != ESP_OK) {
    s_last_error = err;
    ESP_LOGE(TAG, "SPI transmit failed: %s", esp_err_to_name(err));
  }
}

static void cmd(uint8_t c) {
  gpio_set_level(PIN_EPD_DC, 0);
  tx(&c, 1);
}
static void dat(uint8_t d) {
  gpio_set_level(PIN_EPD_DC, 1);
  tx(&d, 1);
}
static void dat_buf(const uint8_t *buf, size_t len) {
  gpio_set_level(PIN_EPD_DC, 1);
  while (len) {                              // DMA 单笔上限,分块发
    size_t n = len > 2048 ? 2048 : len;
    tx(buf, n);
    buf += n; len -= n;
  }
}
static void wait_busy(void) {                // 厂商 Epaper_READBUSY(高=忙),加 10s 超时保护
                                             // (低温下全刷会明显变慢,别收得太紧)
  for (int i = 0; i < 10000; i++) {
    if (gpio_get_level(PIN_EPD_BUSY) == 0) return;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
  ESP_LOGW(TAG, "BUSY 超时");
}
static void hw_reset(void) {                 // 厂商各 Init 开头的复位脉冲
  gpio_set_level(PIN_EPD_RST, 0);
  vTaskDelay(pdMS_TO_TICKS(10));
  gpio_set_level(PIN_EPD_RST, 1);
  vTaskDelay(pdMS_TO_TICKS(10));
  s_sleeping = false;
}

// ---------- 旋转:横屏 fb(250×122, 1=黑) → 面板竖屏缓冲(1=白) ----------
static void rotate_fb(const uint8_t *fb) {
  memset(s_panel_buf, 0xFF, sizeof(s_panel_buf));   // 面板 1=白
  for (int ly = 0; ly < EPD_LANDSCAPE_H; ly++) {
    for (int lx = 0; lx < EPD_LANDSCAPE_W; lx++) {
      if (!(fb[ly * 32 + (lx >> 3)] & (0x80 >> (lx & 7)))) continue;  // 只处理黑点
      // 横屏(lx,ly) → 面板(px,py):默认 px 沿横屏 y、py 沿横屏 x
      int px = EPD_ROT_FLIP_X ? (EPD_LANDSCAPE_H - 1 - ly) : ly;
      int py = EPD_ROT_FLIP_Y ? lx : (EPD_LANDSCAPE_W - 1 - lx);
      px += EPD_X_OFFSET;
      s_panel_buf[py * (EPD_PANEL_W / 8) + (px >> 3)] &= ~(0x80 >> (px & 7));
    }
  }
}

// ---------- 厂商 EPD_HW_Init():全刷初始化 ----------
static void init_full(void) {
  hw_reset();
  wait_busy();
  cmd(0x12);                                 // SWRESET
  vTaskDelay(pdMS_TO_TICKS(10));             // SSD1680/屏厂流程要求的固定复位建立时间
  wait_busy();
  cmd(0x01); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256); dat(0x00);  // 驱动输出
  cmd(0x11); dat(0x01);                      // 数据方向:x增 y减
  cmd(0x44); dat(0x00); dat(EPD_PANEL_W / 8 - 1);
  cmd(0x45); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256); dat(0x00); dat(0x00);
  cmd(0x3C); dat(0x05);                      // 边界波形
  cmd(0x21); dat(0x00); dat(0x80);           // 显示更新控制
  cmd(0x18); dat(0x80);                      // 内置温度传感器
  cmd(0x4E); dat(0x00);
  cmd(0x4F); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256);
  wait_busy();
}

// ---------- 厂商 EPD_HW_Init_Fast():快刷初始化(温度寄存器 0x64 技巧) ----------
static void init_fast(void) {
  hw_reset();
  cmd(0x12);
  vTaskDelay(pdMS_TO_TICKS(10));
  wait_busy();
  cmd(0x18); dat(0x80);
  cmd(0x22); dat(0xB1); cmd(0x20); wait_busy();
  cmd(0x1A); dat(0x64); dat(0x00);
  cmd(0x22); dat(0x91); cmd(0x20); wait_busy();
  // 快刷路径厂商未重设 RAM 窗口(SWRESET 后为全屏默认),补设一次以防万一
  cmd(0x11); dat(0x01);
  cmd(0x44); dat(0x00); dat(EPD_PANEL_W / 8 - 1);
  cmd(0x45); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256); dat(0x00); dat(0x00);
  cmd(0x4E); dat(0x00);
  cmd(0x4F); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256);
}

// ---------- 厂商 EPD_Dis_PartAll() 开头:局刷前置(复位 + 边界 0x80) ----------
static void init_partial(void) {
  hw_reset();
  cmd(0x3C); dat(0x80);
  cmd(0x11); dat(0x01);
  cmd(0x44); dat(0x00); dat(EPD_PANEL_W / 8 - 1);
  cmd(0x45); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256); dat(0x00); dat(0x00);
  cmd(0x4E); dat(0x00);
  cmd(0x4F); dat((EPD_PANEL_H - 1) % 256); dat((EPD_PANEL_H - 1) / 256);
}

static void write_ram_update(uint8_t update_mode) {   // 0x24 写图 + 0x22/0x20 触发
  cmd(0x24); dat_buf(s_panel_buf, sizeof(s_panel_buf));
  cmd(0x22); dat(update_mode);
  cmd(0x20);
  wait_busy();
}

// ---------- 对外接口 ----------
esp_err_t epd_init(void) {
  s_last_error = ESP_OK;
  gpio_config_t out = {
    .pin_bit_mask = (1ULL << PIN_EPD_DC) | (1ULL << PIN_EPD_RST) | (1ULL << PIN_EPD_POWER_EN),
    .mode = GPIO_MODE_OUTPUT,
  };
  esp_err_t err = gpio_config(&out);
  if (err != ESP_OK) return err;
  gpio_config_t in = {.pin_bit_mask = 1ULL << PIN_EPD_BUSY, .mode = GPIO_MODE_INPUT};
  err = gpio_config(&in);
  if (err != ESP_OK) return err;
  gpio_set_level(PIN_EPD_POWER_EN, 0);

  spi_bus_config_t bus = {
    .sclk_io_num = PIN_EPD_SCLK, .mosi_io_num = PIN_EPD_MOSI,
    .miso_io_num = -1, .quadwp_io_num = -1, .quadhd_io_num = -1,
    .max_transfer_sz = 4096,
  };
  err = spi_bus_initialize(EPD_SPI_HOST, &bus, SPI_DMA_CH_AUTO);
  if (err != ESP_OK) return err;
  spi_device_interface_config_t dev = {
    .clock_speed_hz = 2 * 1000 * 1000,       // 诊断/量产共用保守时钟，增加长走线与样板裕量
    .mode = 0, .spics_io_num = PIN_EPD_CS, .queue_size = 4,
  };
  err = spi_bus_add_device(EPD_SPI_HOST, &dev, &s_spi);
  if (err != ESP_OK) {
    spi_bus_free(EPD_SPI_HOST);
    return err;
  }
  return err;
}

esp_err_t epd_last_error(void) { return s_last_error; }

void epd_power(bool on) {
  if (s_powered == on) return;
  gpio_set_level(PIN_EPD_POWER_EN, on);
  if (on) vTaskDelay(pdMS_TO_TICKS(10));
  s_powered = on;
  s_sleeping = false;
}

void epd_frame_full(const uint8_t *fb)  { s_last_error = ESP_OK; rotate_fb(fb); init_full(); write_ram_update(0xF7); }
void epd_frame_fast(const uint8_t *fb)  { s_last_error = ESP_OK; rotate_fb(fb); init_fast(); write_ram_update(0xC7); }

void epd_base_map(const uint8_t *fb) {       // 厂商 EPD_SetRAMValue_BaseMap:0x24 与 0x26 同图
  rotate_fb(fb);
  init_full();
  cmd(0x24); dat_buf(s_panel_buf, sizeof(s_panel_buf));
  cmd(0x26); dat_buf(s_panel_buf, sizeof(s_panel_buf));
  cmd(0x22); dat(0xF7);
  cmd(0x20);
  wait_busy();
}

void epd_frame_partial(const uint8_t *fb) { rotate_fb(fb); init_partial(); write_ram_update(0xFF); }

void epd_deep_sleep(void) {                  // 厂商 EPD_DeepSleep
  if (s_sleeping) return;
  cmd(0x10); dat(0x01);
  vTaskDelay(pdMS_TO_TICKS(100));
  s_sleeping = true;
}
