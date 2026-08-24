// GDEY0154D67 / SSD1681 200x200 monochrome e-paper driver.
// Register setup follows the SSD1681 data sheet and the Good Display D67 flow.
#include "epd_ssd1681.h"
#include "board_pins.h"

#include <string.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "epd154";
static spi_device_handle_t s_spi;
static uint8_t s_panel[EPD_FB_BYTES];       // SSD1681 RAM convention: 0=black, 1=white
static uint8_t s_previous_panel[EPD_FB_BYTES];
static bool s_powered;
static bool s_sleeping;
static bool s_base_ready;
static esp_err_t s_last_error;

static void update(uint8_t mode, const char *stage);

static void tx(const void *buffer, size_t length) {
  if (s_last_error != ESP_OK || !s_spi || !buffer || length == 0) return;
  spi_transaction_t transaction = {
    .length = length * 8,
    .tx_buffer = buffer,
  };
  esp_err_t err = spi_device_polling_transmit(s_spi, &transaction);
  if (err != ESP_OK) {
    s_last_error = err;
    ESP_LOGE(TAG, "SPI transmit failed: %s", esp_err_to_name(err));
  }
}

static void command(uint8_t value) {
  gpio_set_level(PIN_EPD_DC, 0);
  tx(&value, 1);
}

static void data(uint8_t value) {
  gpio_set_level(PIN_EPD_DC, 1);
  tx(&value, 1);
}

static void data_buffer(const uint8_t *buffer, size_t length) {
  gpio_set_level(PIN_EPD_DC, 1);
  while (length > 0) {
    size_t block = length > 2048 ? 2048 : length;
    tx(buffer, block);
    buffer += block;
    length -= block;
  }
}

static bool wait_busy(const char *stage) {
  // SSD1681 BUSY is high while the controller is busy.
  for (int elapsed = 0; elapsed < 12000; elapsed++) {
    if (gpio_get_level(PIN_EPD_BUSY) == 0) return true;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
  ESP_LOGW(TAG, "BUSY timeout at %s", stage ? stage : "unknown");
  s_last_error = ESP_ERR_TIMEOUT;
  return false;
}

static void hardware_reset(void) {
  gpio_set_level(PIN_EPD_RST, 0);
  vTaskDelay(pdMS_TO_TICKS(10));
  gpio_set_level(PIN_EPD_RST, 1);
  vTaskDelay(pdMS_TO_TICKS(10));
  s_sleeping = false;
}

static void set_full_window(void) {
  // Exact GDEY0154D67 vendor addressing: X increments while Y decrements.
  // The controller starts at the bottom gate (199) and consumes 5000 bytes.
  command(0x11); data(0x01);                 // X+, Y-
  command(0x44); data(0x00); data(0x18);    // 25 bytes: 0..24
  command(0x45);
  data(0xC7); data(0x00); data(0x00); data(0x00); // Y: 199..0
  command(0x3C); data(0x05);                 // border waveform
  command(0x18); data(0x80);                 // internal temperature sensor
  command(0x4E); data(0x00);
  command(0x4F); data(0xC7); data(0x00);
  wait_busy("set-address");
}

static void init_display(void) {
  hardware_reset();
  wait_busy("power-on-reset");
  command(0x12);                             // SWRESET
  wait_busy("reset");
  command(0x01);                             // 200 gate outputs
  data(0xC7); data(0x00); data(0x00);
  set_full_window();
}

static void init_display_fast(void) {
  // Exact GDEY0154D67 vendor fast-refresh initialization.  The preceding
  // full frame establishes the controller window and waveform base; mode-1
  // deep sleep retains the required controller state between updates.
  hardware_reset();
  command(0x12);                             // SWRESET
  wait_busy("fast-reset");
  command(0x18); data(0x80);                 // internal temperature sensor
  update(0xB1, "fast-load-temperature");
  command(0x1A); data(0x64); data(0x00);     // vendor fast-temperature value
  update(0x91, "fast-apply-temperature");
}

static void map_framebuffer(const uint8_t *fb) {
  memset(s_panel, 0xFF, sizeof(s_panel));
  if (!fb) return;
  for (int y = 0; y < EPD_LANDSCAPE_H; y++) {
    for (int x = 0; x < EPD_LANDSCAPE_W; x++) {
      if ((fb[y * EPD_FB_STRIDE + (x >> 3)] & (0x80U >> (x & 7))) == 0) continue;
      int px = x;
      int py = y;
#if EPD_SWAP_XY
      int swap = px; px = py; py = swap;
#endif
#if EPD_FLIP_X
      px = EPD_LANDSCAPE_W - 1 - px;
#endif
#if EPD_FLIP_Y
      py = EPD_LANDSCAPE_H - 1 - py;
#endif
      s_panel[py * EPD_FB_STRIDE + (px >> 3)] &= (uint8_t)~(0x80U >> (px & 7));
    }
  }
}

static void write_ram_buffer(uint8_t ram_command, const uint8_t *buffer) {
  command(ram_command);
  data_buffer(buffer, EPD_FB_BYTES);
}

static void write_ram(uint8_t ram_command) { write_ram_buffer(ram_command, s_panel); }

static void transform_point(int x, int y, int *panel_x, int *panel_y) {
  int px = x;
  int py = y;
#if EPD_SWAP_XY
  int swap = px; px = py; py = swap;
#endif
#if EPD_FLIP_X
  px = EPD_LANDSCAPE_W - 1 - px;
#endif
#if EPD_FLIP_Y
  py = EPD_LANDSCAPE_H - 1 - py;
#endif
  *panel_x = px;
  *panel_y = py;
}

static void set_partial_window(int panel_x0, int panel_y0,
                               int panel_x1, int panel_y1) {
  int byte_x0 = panel_x0 >> 3;
  int byte_x1 = panel_x1 >> 3;
  // s_panel row zero is streamed to controller gate 199 because the approved
  // panel orientation uses X+, Y-. Keep this addressing identical to the
  // vendor full-frame sequence.
  int controller_y0 = EPD_LANDSCAPE_H - 1 - panel_y0;
  int controller_y1 = EPD_LANDSCAPE_H - 1 - panel_y1;
  command(0x11); data(0x01);                 // X+, Y-
  command(0x3C); data(0x80);                 // vendor partial border waveform
  command(0x44); data((uint8_t)byte_x0); data((uint8_t)byte_x1);
  command(0x45);
  data((uint8_t)(controller_y0 & 0xFF));
  data((uint8_t)((controller_y0 >> 8) & 0xFF));
  data((uint8_t)(controller_y1 & 0xFF));
  data((uint8_t)((controller_y1 >> 8) & 0xFF));
  command(0x4E); data((uint8_t)byte_x0);
  command(0x4F);
  data((uint8_t)(controller_y0 & 0xFF));
  data((uint8_t)((controller_y0 >> 8) & 0xFF));
}

static void write_partial_ram(uint8_t ram_command, int panel_x0, int panel_y0,
                              int panel_x1, int panel_y1) {
  int byte_x0 = panel_x0 >> 3;
  int byte_x1 = panel_x1 >> 3;
  size_t bytes_per_row = (size_t)(byte_x1 - byte_x0 + 1);
  command(ram_command);
  for (int row = panel_y0; row <= panel_y1; ++row) {
    data_buffer(&s_panel[row * EPD_FB_STRIDE + byte_x0], bytes_per_row);
  }
}

static uint32_t frame_hash_length(const uint8_t *buffer, size_t length) {
  uint32_t hash = 2166136261U;
  for (size_t i = 0; i < length; ++i) {
    hash ^= buffer[i];
    hash *= 16777619U;
  }
  return hash;
}

static uint32_t frame_hash(const uint8_t *buffer) {
  return frame_hash_length(buffer, EPD_FB_BYTES);
}

static void update(uint8_t mode, const char *stage) {
  command(0x22);
  data(mode);
  command(0x20);
  wait_busy(stage);
}

esp_err_t epd_init(void) {
  s_last_error = ESP_OK;
  gpio_config_t outputs = {
    .pin_bit_mask = (1ULL << PIN_EPD_DC) | (1ULL << PIN_EPD_RST) |
                    (1ULL << PIN_EPD_POWER_EN),
    .mode = GPIO_MODE_OUTPUT,
  };
  esp_err_t err = gpio_config(&outputs);
  if (err != ESP_OK) return err;
  gpio_config_t busy = {
    .pin_bit_mask = 1ULL << PIN_EPD_BUSY,
    .mode = GPIO_MODE_INPUT,
  };
  err = gpio_config(&busy);
  if (err != ESP_OK) return err;
  gpio_set_level(PIN_EPD_POWER_EN, 0);

  spi_bus_config_t bus = {
    .sclk_io_num = PIN_EPD_SCLK,
    .mosi_io_num = PIN_EPD_MOSI,
    .miso_io_num = -1,
    .quadwp_io_num = -1,
    .quadhd_io_num = -1,
    .max_transfer_sz = 6144,
  };
  err = spi_bus_initialize(EPD_SPI_HOST, &bus, SPI_DMA_CH_AUTO);
  if (err != ESP_OK) return err;
  spi_device_interface_config_t device = {
    .clock_speed_hz = 2 * 1000 * 1000,
    .mode = 0,
    .spics_io_num = PIN_EPD_CS,
    .queue_size = 4,
  };
  err = spi_bus_add_device(EPD_SPI_HOST, &device, &s_spi);
  if (err != ESP_OK) {
    spi_bus_free(EPD_SPI_HOST);
    return err;
  }
  return ESP_OK;
}

esp_err_t epd_last_error(void) { return s_last_error; }

void epd_power(bool on) {
  if (s_powered == on) return;
  gpio_set_level(PIN_EPD_POWER_EN, on ? 1 : 0);
  if (on) vTaskDelay(pdMS_TO_TICKS(10));
  else s_base_ready = false;
  s_powered = on;
  s_sleeping = false;
}

void epd_frame_full(const uint8_t *fb) {
  s_last_error = ESP_OK;
  map_framebuffer(fb);
  init_display();
  // Monochrome path: one 1-bit plane in SSD1681 RAM 0x24.
  write_ram(0x24);
  set_full_window();
  write_ram(0x26);                            // establish partial-update base
  update(0xF7, "full");
  s_base_ready = s_last_error == ESP_OK;
  ESP_LOGI(TAG, "LY|EPD|refresh=vendor-full result=%s frame=%08lx",
           esp_err_to_name(s_last_error), (unsigned long)frame_hash(s_panel));
}

void epd_frame_fast(const uint8_t *fb) {
  if (!s_base_ready) {
    epd_frame_full(fb);                       // required once after panel power-on
    return;
  }
  s_last_error = ESP_OK;
  map_framebuffer(fb);
  init_display_fast();
  set_full_window();                          // partial windows must not leak
  command(0x24);
  data_buffer(s_panel, EPD_FB_BYTES);
  update(0xC7, "fast-refresh");
  if (s_last_error == ESP_OK) {
    set_full_window();
    write_ram(0x26);                          // new frame is next partial base
  }
  if (s_last_error != ESP_OK) s_base_ready = false;
  ESP_LOGI(TAG, "LY|EPD|refresh=vendor-fast result=%s frame=%08lx",
           esp_err_to_name(s_last_error), (unsigned long)frame_hash(s_panel));
}

void epd_frame_partial_window(const uint8_t *fb, uint16_t x, uint16_t y,
                              uint16_t width, uint16_t height) {
  if (!fb || width == 0 || height == 0 || x >= EPD_LANDSCAPE_W ||
      y >= EPD_LANDSCAPE_H) return;
  if ((uint32_t)x + width > EPD_LANDSCAPE_W) width = EPD_LANDSCAPE_W - x;
  if ((uint32_t)y + height > EPD_LANDSCAPE_H) height = EPD_LANDSCAPE_H - y;
  if (!s_base_ready) {
    epd_frame_full(fb);
    return;
  }

  s_last_error = ESP_OK;
  memcpy(s_previous_panel, s_panel, sizeof(s_previous_panel));
  map_framebuffer(fb);
  int corners_x[4], corners_y[4];
  transform_point(x, y, &corners_x[0], &corners_y[0]);
  transform_point(x + width - 1, y, &corners_x[1], &corners_y[1]);
  transform_point(x, y + height - 1, &corners_x[2], &corners_y[2]);
  transform_point(x + width - 1, y + height - 1,
                  &corners_x[3], &corners_y[3]);
  int panel_x0 = corners_x[0], panel_x1 = corners_x[0];
  int panel_y0 = corners_y[0], panel_y1 = corners_y[0];
  for (int i = 1; i < 4; ++i) {
    if (corners_x[i] < panel_x0) panel_x0 = corners_x[i];
    if (corners_x[i] > panel_x1) panel_x1 = corners_x[i];
    if (corners_y[i] < panel_y0) panel_y0 = corners_y[i];
    if (corners_y[i] > panel_y1) panel_y1 = corners_y[i];
  }
  // SSD1681 X addresses are byte based. Expand to byte boundaries without
  // changing any pixels outside the requested logical rectangle in s_panel.
  panel_x0 &= ~7;
  panel_x1 |= 7;
  if (panel_x1 >= EPD_LANDSCAPE_W) panel_x1 = EPD_LANDSCAPE_W - 1;

  /* map_framebuffer builds a complete candidate frame. A window update must
     keep the authoritative panel shadow unchanged everywhere else. */
  int byte_x0 = panel_x0 >> 3;
  int byte_x1 = panel_x1 >> 3;
  for (int row = 0; row < EPD_LANDSCAPE_H; ++row) {
    for (int xb = 0; xb < EPD_FB_STRIDE; ++xb) {
      if (row >= panel_y0 && row <= panel_y1 &&
          xb >= byte_x0 && xb <= byte_x1) continue;
      size_t index = (size_t)row * EPD_FB_STRIDE + (size_t)xb;
      s_panel[index] = s_previous_panel[index];
    }
  }

  hardware_reset();
  set_partial_window(panel_x0, panel_y0, panel_x1, panel_y1);
  write_partial_ram(0x24, panel_x0, panel_y0, panel_x1, panel_y1);
  update(0xFF, "partial-refresh");
  if (s_last_error == ESP_OK) {
    // Keep RAM 0x26 synchronized so the next partial waveform compares
    // against the frame that is actually visible on the panel.
    set_partial_window(panel_x0, panel_y0, panel_x1, panel_y1);
    write_partial_ram(0x26, panel_x0, panel_y0, panel_x1, panel_y1);
  } else {
    memcpy(s_panel, s_previous_panel, sizeof(s_panel));
    s_base_ready = false;
  }
  ESP_LOGI(TAG,
           "LY|EPD|refresh=vendor-partial result=%s logical=%u,%u,%u,%u panel=%d,%d,%d,%d frame=%08lx",
           esp_err_to_name(s_last_error), (unsigned)x, (unsigned)y,
           (unsigned)width, (unsigned)height, panel_x0, panel_y0,
           panel_x1, panel_y1, (unsigned long)frame_hash(s_panel));
}

void epd_frame_partial_auto(const uint8_t *fb) {
  if (!fb) return;
  if (!s_base_ready) {
    epd_frame_full(fb);
    return;
  }

  /* s_panel is the last successfully displayed physical frame. Preserve it
     while mapping the new logical frame, then find the exact changed bounds. */
  memcpy(s_previous_panel, s_panel, sizeof(s_previous_panel));
  map_framebuffer(fb);
  int x0 = EPD_LANDSCAPE_W;
  int y0 = EPD_LANDSCAPE_H;
  int x1 = -1;
  int y1 = -1;
  for (int y = 0; y < EPD_LANDSCAPE_H; ++y) {
    for (int xb = 0; xb < EPD_FB_STRIDE; ++xb) {
      size_t index = (size_t)y * EPD_FB_STRIDE + (size_t)xb;
      if (s_previous_panel[index] == s_panel[index]) continue;
      int left = xb * 8;
      int right = left + 7;
      if (left < x0) x0 = left;
      if (right > x1) x1 = right;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
    }
  }
  if (x1 < x0 || y1 < y0) {
    s_last_error = ESP_OK;
    ESP_LOGI(TAG, "LY|EPD|refresh=vendor-partial-auto result=no-change");
    return;
  }
  if (x1 >= EPD_LANDSCAPE_W) x1 = EPD_LANDSCAPE_W - 1;

  s_last_error = ESP_OK;
  hardware_reset();
  set_partial_window(x0, y0, x1, y1);
  write_partial_ram(0x24, x0, y0, x1, y1);
  update(0xFF, "partial-auto-refresh");
  if (s_last_error == ESP_OK) {
    set_partial_window(x0, y0, x1, y1);
    write_partial_ram(0x26, x0, y0, x1, y1);
  } else {
    memcpy(s_panel, s_previous_panel, sizeof(s_panel));
    s_base_ready = false;
  }
  ESP_LOGI(TAG,
           "LY|EPD|refresh=vendor-partial-auto result=%s panel=%d,%d,%d,%d frame=%08lx",
           esp_err_to_name(s_last_error), x0, y0, x1, y1,
           (unsigned long)frame_hash(s_panel));
}

void epd_deep_sleep(void) {
  if (!s_powered || s_sleeping || !s_spi) return;
  command(0x10); data(0x01);
  vTaskDelay(pdMS_TO_TICKS(100));
  s_sleeping = true;
}
