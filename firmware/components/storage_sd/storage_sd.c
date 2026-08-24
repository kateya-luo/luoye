#include "storage_sd.h"
#include "storage_format.h"
#include "upload_store.h"
#include "audio_pdm.h"
#include "power_mgr.h"
#include "board_pins.h"

#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/sdspi_host.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "sdmmc_cmd.h"

static const char *TAG = "sd";
#define MOUNT_POINT "/sdcard"
#define SESSION_ROOT MOUNT_POINT "/rec"
#define DIAG_ROOT MOUNT_POINT "/diag"
#define POWER_DIAG_PATH DIAG_ROOT "/power.csv"
#define CARD_META_PATH MOUNT_POINT "/luoye-card.json"
#define CARD_PROBE_PATH MOUNT_POINT "/.luoye-write-test.tmp"
#define CARD_SPEED_PROBE_PATH MOUNT_POINT "/.luoye-speed-test.tmp"
#define CARD_SCHEMA "luoye-storage/2"
#define WRITE_SAMPLES 2048
#define SYNC_INTERVAL_BYTES (32U * 1024U)
#define SESSION_DIR_BYTES 160
#define SESSION_ID_BYTES 48
#define JSON_PATH_BYTES 208
#define SD_MOUNT_ATTEMPTS 4
#define SD_SPI_TRANSFER_BYTES (64U * 1024U)
#define SD_SPI_FREQUENCY_KHZ SDMMC_FREQ_DEFAULT
#define SD_DMA_READ_BYTES (16U * 1024U)
#define SD_DMA_LOW_WATER_BYTES (8U * 1024U)
#define SD_DMA_LOW_WATER_WAIT_MS 20U
#define SD_DMA_LOW_WATER_TIMEOUT_MS 30000U
#define SD_DMA_PREPARE_GUARD_BYTES (16U * 1024U)

static sdmmc_card_t *s_card;
static uint32_t s_sd_freq_khz;
static storage_post_fn s_post;
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_power_diag_lock;
static SemaphoreHandle_t s_dma_read_lock;
static bool s_recovery_blocked;
static bool s_storage_ready;
static bool s_format_in_progress;

static esp_err_t wait_for_sd_dma_headroom(void) {
  int64_t started_us = esp_timer_get_time();
  bool waiting = false;
  while (true) {
    size_t free_dma = heap_caps_get_free_size(MALLOC_CAP_INTERNAL |
                                              MALLOC_CAP_DMA);
    size_t largest_dma = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                          MALLOC_CAP_DMA);
    if (largest_dma >= SD_DMA_LOW_WATER_BYTES) {
      if (waiting) {
        ESP_LOGI(TAG,
                 "LY|STORAGE_DMA|event=backpressure state=recovered waited_ms=%llu free_dma=%u largest_dma=%u low_water=%u",
                 (unsigned long long)((esp_timer_get_time() - started_us) / 1000),
                 (unsigned)free_dma, (unsigned)largest_dma,
                 (unsigned)SD_DMA_LOW_WATER_BYTES);
      }
      return ESP_OK;
    }
    uint64_t waited_ms = (uint64_t)(esp_timer_get_time() - started_us) / 1000U;
    if (!waiting) {
      waiting = true;
      ESP_LOGW(TAG,
               "LY|STORAGE_DMA|event=backpressure state=wait free_dma=%u largest_dma=%u low_water=%u",
               (unsigned)free_dma, (unsigned)largest_dma,
               (unsigned)SD_DMA_LOW_WATER_BYTES);
    }
    if (waited_ms >= SD_DMA_LOW_WATER_TIMEOUT_MS) {
      ESP_LOGE(TAG,
               "LY|STORAGE_DMA|event=backpressure state=timeout waited_ms=%llu free_dma=%u largest_dma=%u low_water=%u",
               (unsigned long long)waited_ms, (unsigned)free_dma,
               (unsigned)largest_dma, (unsigned)SD_DMA_LOW_WATER_BYTES);
      return ESP_ERR_NO_MEM;
    }
    vTaskDelay(pdMS_TO_TICKS(SD_DMA_LOW_WATER_WAIT_MS));
  }
}

esp_err_t storage_sd_read(FILE *file, void *buffer, size_t wanted,
                          size_t *received) {
  if (!file || !buffer || !received || !s_dma_read_lock) {
    return ESP_ERR_INVALID_STATE;
  }
  *received = 0;
  if (!wanted) return ESP_OK;
  int fd = fileno(file);
  if (fd < 0) return ESP_ERR_INVALID_ARG;
  if (xSemaphoreTake(s_dma_read_lock, portMAX_DELAY) != pdTRUE) {
    return ESP_ERR_TIMEOUT;
  }
  uint8_t *destination = (uint8_t *)buffer;
  esp_err_t result = ESP_OK;
  while (*received < wanted) {
    size_t remaining = wanted - *received;
    size_t chunk = remaining < SD_DMA_READ_BYTES
                     ? remaining : SD_DMA_READ_BYTES;
    result = wait_for_sd_dma_headroom();
    if (result != ESP_OK) break;
    /* The stock SDSPI driver owns its DMA block buffer and copies into this
       caller buffer. A second 16 KiB internal staging area only fragments the
       DMA heap, so read directly into internal RAM or PSRAM. */
    ssize_t count;
    do {
      count = read(fd, destination + *received, chunk);
    } while (count < 0 && errno == EINTR);
    if (count <= 0) {
      result = ESP_FAIL;
      break;
    }
    *received += (size_t)count;
    if ((size_t)count != chunk) {
      result = ESP_FAIL;
      break;
    }
  }
  xSemaphoreGive(s_dma_read_lock);
  return result == ESP_OK && *received == wanted ? ESP_OK : ESP_FAIL;
}

esp_err_t storage_sd_seek(FILE *file, uint32_t offset) {
  if (!file || offset > (uint32_t)LONG_MAX) return ESP_ERR_INVALID_ARG;
  int fd = fileno(file);
  if (fd < 0) return ESP_ERR_INVALID_ARG;
  off_t positioned;
  do {
    positioned = lseek(fd, (off_t)offset, SEEK_SET);
  } while (positioned < 0 && errno == EINTR);
  return positioned == (off_t)offset ? ESP_OK : ESP_FAIL;
}

esp_err_t storage_sd_size(FILE *file, size_t *size_out) {
  if (!file || !size_out) return ESP_ERR_INVALID_ARG;
  int fd = fileno(file);
  struct stat info;
  if (fd < 0 || fstat(fd, &info) != 0 || info.st_size < 0 ||
      (uint64_t)info.st_size > SIZE_MAX) {
    return ESP_FAIL;
  }
  *size_out = (size_t)info.st_size;
  return ESP_OK;
}

esp_err_t storage_sd_prepare_range(FILE *file, uint32_t file_offset) {
  if (!file || file_offset > (uint32_t)LONG_MAX) return ESP_ERR_INVALID_ARG;
  size_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL |
                                                 MALLOC_CAP_8BIT);
  size_t largest_dma = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                        MALLOC_CAP_DMA);
  if (largest_dma < SD_DMA_PREPARE_GUARD_BYTES) {
    ESP_LOGW(TAG,
             "LY|STORAGE_DMA|event=range_prepare offset=%lu result=defer free_internal=%u largest_dma=%u guard=%u",
             (unsigned long)file_offset, (unsigned)free_internal,
             (unsigned)largest_dma, (unsigned)SD_DMA_PREPARE_GUARD_BYTES);
    return ESP_ERR_NO_MEM;
  }
  if (storage_sd_seek(file, file_offset) != ESP_OK) return ESP_FAIL;

  /* WAV PCM begins at byte 44, so the first FatFS read is not sector aligned.
     Prime that sector before esp_http_client_open() grows the TCP/HTTP working
     set. CONFIG_FATFS_ALLOC_PREFER_EXTRAM is enabled, while the official SDSPI
     driver uses its own DMA-capable bounce buffer when one is required. */
  uint8_t first_byte = 0;
  size_t received = 0;
  esp_err_t result = storage_sd_read(file, &first_byte, 1, &received);
  if (result == ESP_OK && received == 1 &&
      storage_sd_seek(file, file_offset) == ESP_OK) {
    ESP_LOGI(TAG,
             "LY|STORAGE_DMA|event=range_prepare offset=%lu result=ready free_internal=%u largest_dma=%u",
             (unsigned long)file_offset, (unsigned)free_internal,
             (unsigned)largest_dma);
    return ESP_OK;
  }
  ESP_LOGW(TAG,
           "LY|STORAGE_DMA|event=range_prepare offset=%lu result=failed esp=%s received=%u errno=%d",
           (unsigned long)file_offset, esp_err_to_name(result),
           (unsigned)received, errno);
  return result == ESP_OK ? ESP_FAIL : result;
}

static void digest_to_hex(const unsigned char digest[32], char hex[65]) {
  static const char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < 32; ++i) {
    hex[i * 2] = digits[digest[i] >> 4];
    hex[i * 2 + 1] = digits[digest[i] & 0x0FU];
  }
  hex[64] = '\0';
}

static bool power_csv_digest(FILE *file, unsigned long *size_out,
                             char sha256_hex[65]) {
  unsigned char buffer[512];
  unsigned char digest[32];
  unsigned long total = 0;
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  bool ok = mbedtls_sha256_starts(&sha, 0) == 0;
  while (ok) {
    size_t count = fread(buffer, 1, sizeof(buffer), file);
    if (count > 0) {
      total += (unsigned long)count;
      ok = mbedtls_sha256_update(&sha, buffer, count) == 0;
    }
    if (count < sizeof(buffer)) {
      if (ferror(file)) ok = false;
      break;
    }
  }
  if (ok) ok = mbedtls_sha256_finish(&sha, digest) == 0;
  mbedtls_sha256_free(&sha);
  if (!ok) return false;
  digest_to_hex(digest, sha256_hex);
  *size_out = total;
  return true;
}

static void power_csv_export(bool include_data) {
  if (!s_card || s_recovery_blocked) {
    printf("LY|SD_EXPORT|event=error result=unavailable reason=sd_not_ready\n");
    fflush(stdout);
    return;
  }
  if (sd_session_is_open()) {
    printf("LY|SD_EXPORT|event=error result=busy reason=recording_active\n");
    fflush(stdout);
    return;
  }
  if (xSemaphoreTake(s_power_diag_lock, pdMS_TO_TICKS(5000)) != pdTRUE) {
    printf("LY|SD_EXPORT|event=error result=busy reason=diag_writer_busy\n");
    fflush(stdout);
    return;
  }

  FILE *file = fopen(POWER_DIAG_PATH, "rb");
  if (!file) {
    printf("LY|SD_EXPORT|event=error result=not_found reason=power_csv_missing\n");
    fflush(stdout);
    xSemaphoreGive(s_power_diag_lock);
    return;
  }
  unsigned long bytes = 0;
  char sha256_hex[65];
  if (!power_csv_digest(file, &bytes, sha256_hex) || fseek(file, 0, SEEK_SET) != 0) {
    printf("LY|SD_EXPORT|event=error result=read_failed reason=digest_failed\n");
    fflush(stdout);
    fclose(file);
    xSemaphoreGive(s_power_diag_lock);
    return;
  }
  if (!include_data) {
    printf("LY|SD_EXPORT|event=info result=ok path=diag/power.csv bytes=%lu sha256=%s\n",
           bytes, sha256_hex);
    fflush(stdout);
    fclose(file);
    xSemaphoreGive(s_power_diag_lock);
    return;
  }

  enum { RAW_CHUNK_BYTES = 192, BASE64_BYTES = 260 };
  unsigned char raw[RAW_CHUNK_BYTES];
  unsigned char encoded[BASE64_BYTES];
  unsigned long sent = 0;
  unsigned long sequence = 0;
  bool ok = true;
  printf("LY|SD_EXPORT|event=begin result=ok path=diag/power.csv bytes=%lu sha256=%s encoding=base64 chunk_bytes=%u\n",
         bytes, sha256_hex, RAW_CHUNK_BYTES);
  fflush(stdout);
  while (ok) {
    if (sd_session_is_open()) {
      ok = false;
      break;
    }
    size_t count = fread(raw, 1, sizeof(raw), file);
    if (count > 0) {
      size_t encoded_bytes = 0;
      if (mbedtls_base64_encode(encoded, sizeof(encoded) - 1, &encoded_bytes,
                                raw, count) != 0) {
        ok = false;
        break;
      }
      encoded[encoded_bytes] = '\0';
      printf("LY|SD_EXPORT_DATA|seq=%lu|%s\n", sequence, (char *)encoded);
      sent += (unsigned long)count;
      sequence++;
      if ((sequence & 0x0FU) == 0) fflush(stdout);
    }
    if (count < sizeof(raw)) {
      if (ferror(file)) ok = false;
      break;
    }
  }
  fflush(stdout);
  fclose(file);
  if (ok && sent == bytes) {
    printf("LY|SD_EXPORT|event=end result=ok path=diag/power.csv bytes=%lu chunks=%lu sha256=%s\n",
           sent, sequence, sha256_hex);
  } else {
    printf("LY|SD_EXPORT|event=end result=error reason=%s bytes=%lu chunks=%lu\n",
           sd_session_is_open() ? "recording_started" : "stream_failed",
           sent, sequence);
  }
  fflush(stdout);
  xSemaphoreGive(s_power_diag_lock);
}

static void usb_command_task(void *arg) {
  (void)arg;
  char command[48];
  size_t length = 0;
  printf("LY|SD_EXPORT|event=ready commands=power_info,power_export\n");
  fflush(stdout);
  for (;;) {
    char value = 0;
    int received = usb_serial_jtag_read_bytes(&value, 1, pdMS_TO_TICKS(1000));
    if (received <= 0) continue;
    if (value == '\r' || value == '\n') {
      if (length == 0) continue;
      command[length] = '\0';
      if (strcmp(command, "power_export") == 0) {
        power_csv_export(true);
      } else if (strcmp(command, "power_info") == 0) {
        power_csv_export(false);
      } else if (strcmp(command, "power_help") == 0 ||
                 strcmp(command, "help") == 0) {
        printf("LY|SD_EXPORT|event=help commands=power_info,power_export note=read_only_no_recording\n");
        fflush(stdout);
      } else {
        printf("LY|SD_EXPORT|event=error result=bad_command command=%s\n", command);
        fflush(stdout);
      }
      length = 0;
    } else if (value >= 0x20 && value <= 0x7E) {
      if (length + 1 < sizeof(command)) command[length++] = value;
      else length = 0;
    }
  }
}

/*
 * The card is soldered in, but its internal power-on reset can occasionally
 * lag behind the ESP32-S3.  Keep CS inactive and provide the >=74 clocks
 * required by the SD SPI entry sequence before every mount attempt.
 *
 * Do this before spi_bus_initialize(); afterwards the SPI peripheral owns the
 * pins.  A failed mount is followed by spi_bus_free(), so the next attempt is
 * a genuinely cold host transaction rather than a continuation of a damaged
 * command stream.
 */
static void sd_spi_bus_idle_clocks(void) {
  gpio_reset_pin(PIN_SD_CS);
  gpio_reset_pin(PIN_SD_SCK);
  gpio_reset_pin(PIN_SD_MOSI);
  gpio_reset_pin(PIN_SD_MISO);
  gpio_set_direction(PIN_SD_CS, GPIO_MODE_OUTPUT);
  gpio_set_direction(PIN_SD_SCK, GPIO_MODE_OUTPUT);
  gpio_set_direction(PIN_SD_MOSI, GPIO_MODE_OUTPUT);
  gpio_set_direction(PIN_SD_MISO, GPIO_MODE_INPUT);
  gpio_set_pull_mode(PIN_SD_MISO, GPIO_PULLUP_ONLY);
  gpio_set_level(PIN_SD_CS, 1);
  gpio_set_level(PIN_SD_MOSI, 1);
  gpio_set_level(PIN_SD_SCK, 0);
  esp_rom_delay_us(10);
  for (unsigned i = 0; i < 80; ++i) {
    gpio_set_level(PIN_SD_SCK, 1);
    esp_rom_delay_us(2);
    gpio_set_level(PIN_SD_SCK, 0);
    esp_rom_delay_us(2);
  }
  gpio_set_level(PIN_SD_CS, 1);
}

static esp_err_t sd_mount_with_retry(bool allow_format) {
  static const uint32_t wait_ms[SD_MOUNT_ATTEMPTS] = {200, 250, 500, 1000};
  /* Fixed 20 MHz policy: every retry uses the proven-safe bus rate.  Do not
     briefly switch a marginal card to 40 MHz before falling back. */
  esp_err_t last_error = ESP_FAIL;

  for (unsigned attempt = 0; attempt < SD_MOUNT_ATTEMPTS; ++attempt) {
    uint32_t frequency = SD_SPI_FREQUENCY_KHZ;
    vTaskDelay(pdMS_TO_TICKS(wait_ms[attempt]));
    sd_spi_bus_idle_clocks();

    spi_bus_config_t bus = {
      .sclk_io_num = PIN_SD_SCK,
      .mosi_io_num = PIN_SD_MOSI,
      .miso_io_num = PIN_SD_MISO,
      .quadwp_io_num = -1,
      .quadhd_io_num = -1,
      .max_transfer_sz = SD_SPI_TRANSFER_BYTES,
    };
    last_error = spi_bus_initialize(SD_SPI_HOST, &bus, SPI_DMA_CH_AUTO);
    if (last_error != ESP_OK) {
      ESP_LOGW(TAG, "LY|STORAGE|event=bus_init_retry attempt=%u/%u esp=%s",
               attempt + 1, SD_MOUNT_ATTEMPTS, esp_err_to_name(last_error));
      continue;
    }

    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = PIN_SD_CS;
    slot.host_id = SD_SPI_HOST;
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    host.slot = SD_SPI_HOST;
    host.max_freq_khz = frequency;
    esp_vfs_fat_sdmmc_mount_config_t mount = {
      /* Formatting is enabled only from storage_sd_format(), after an explicit
         long-press confirmation. Normal boot and all I/O retries are read-only. */
      .format_if_mount_failed = allow_format,
      .max_files = 8,
      .allocation_unit_size = 32 * 1024,
    };
    s_card = NULL;
    last_error = esp_vfs_fat_sdspi_mount(MOUNT_POINT, &host, &slot, &mount,
                                         &s_card);
    if (last_error == ESP_OK) {
      s_sd_freq_khz = frequency;
      ESP_LOGI(TAG,
               "LY|STORAGE|event=mount_ok attempt=%u/%u freq_khz=%lu transfer=%u",
               attempt + 1, SD_MOUNT_ATTEMPTS,
               (unsigned long)s_sd_freq_khz, (unsigned)SD_SPI_TRANSFER_BYTES);
      return ESP_OK;
    }

    s_card = NULL;
    esp_err_t free_error = spi_bus_free(SD_SPI_HOST);
    ESP_LOGW(TAG,
             "LY|STORAGE|event=mount_retry attempt=%u/%u freq_khz=%lu esp=%s bus_free=%s",
             attempt + 1, SD_MOUNT_ATTEMPTS,
             (unsigned long)frequency, esp_err_to_name(last_error),
             esp_err_to_name(free_error));
  }
  return last_error;
}

static struct {
  FILE *wav;
  char dir[SESSION_DIR_BYTES];
  char id[SESSION_ID_BYTES];
  char title[48];
  app_scene_t scene;
  uint32_t data_bytes;
  uint32_t committed_bytes;
  mbedtls_sha256_context range_sha;
  uint32_t range_sha_offset;
  uint32_t range_sha_bytes;
  uint32_t pending_sha_offset;
  uint32_t pending_sha_length;
  char pending_sha256[SD_UPLOAD_SHA256_HEX_BYTES];
  bool range_sha_enabled;
  bool range_sha_active;
  bool pending_sha_ready;
  app_close_reason_t close_reason;
  app_error_t error;
  volatile bool open;
  volatile bool close_req;
  volatile bool closed;
  volatile bool error_posted;
} s_sess;

static app_error_t errno_to_write_error(void) {
  return errno == ENOSPC ? APP_ERR_SD_FULL : APP_ERR_STORAGE_WRITE;
}

static bool make_dir(const char *path) {
  return mkdir(path, 0775) == 0 || errno == EEXIST;
}

static bool file_write_all(FILE *file, const void *data, size_t bytes) {
  if (!file || (!data && bytes)) return false;
  return bytes == 0 || fwrite(data, 1, bytes, file) == bytes;
}

static app_error_t file_sync(FILE *file) {
  if (!file) return APP_ERR_STORAGE_SYNC;
  if (fflush(file) != 0) return errno_to_write_error();
  int fd = fileno(file);
  if (fd < 0 || fsync(fd) != 0) {
    return errno == ENOSPC ? APP_ERR_SD_FULL : APP_ERR_STORAGE_SYNC;
  }
  return APP_ERR_NONE;
}

static app_error_t file_sync_close(FILE *file) {
  app_error_t result = file_sync(file);
  if (fclose(file) != 0 && result == APP_ERR_NONE) {
    result = errno == ENOSPC ? APP_ERR_SD_FULL : APP_ERR_STORAGE_CLOSE;
  }
  return result;
}

static app_error_t range_sha_start(void) {
  mbedtls_sha256_init(&s_sess.range_sha);
  if (mbedtls_sha256_starts(&s_sess.range_sha, false) != 0) {
    mbedtls_sha256_free(&s_sess.range_sha);
    return APP_ERR_STORAGE_WRITE;
  }
  s_sess.range_sha_enabled = true;
  s_sess.range_sha_active = true;
  return APP_ERR_NONE;
}

static void range_sha_disable(const char *phase, app_error_t error) {
  if (s_sess.range_sha_active) {
    mbedtls_sha256_free(&s_sess.range_sha);
  }
  s_sess.range_sha_enabled = false;
  s_sess.range_sha_active = false;
  s_sess.pending_sha_ready = false;
  ESP_LOGW(TAG,
           "LY|STORAGE_SHA|event=disabled id=%s phase=%s code=%d upload_fallback=scan",
           s_sess.id, phase ? phase : "unknown", (int)error);
}

static app_error_t range_sha_finish(bool restart) {
  if (!s_sess.range_sha_enabled) return APP_ERR_NONE;
  if (!s_sess.range_sha_active || !s_sess.range_sha_bytes) {
    return APP_ERR_NONE;
  }
  if (s_sess.pending_sha_ready) return APP_ERR_STORAGE_SYNC;
  unsigned char digest[32];
  if (mbedtls_sha256_finish(&s_sess.range_sha, digest) != 0) {
    return APP_ERR_STORAGE_WRITE;
  }
  mbedtls_sha256_free(&s_sess.range_sha);
  s_sess.range_sha_active = false;
  s_sess.pending_sha_offset = s_sess.range_sha_offset;
  s_sess.pending_sha_length = s_sess.range_sha_bytes;
  digest_to_hex(digest, s_sess.pending_sha256);
  s_sess.pending_sha_ready = true;
  s_sess.range_sha_offset += s_sess.range_sha_bytes;
  s_sess.range_sha_bytes = 0;
  return restart ? range_sha_start() : APP_ERR_NONE;
}

static app_error_t range_sha_feed(const void *data, size_t bytes) {
  if (!s_sess.range_sha_enabled) return APP_ERR_NONE;
  if ((!data && bytes) || !s_sess.range_sha_active) {
    return APP_ERR_STORAGE_WRITE;
  }
  const uint8_t *cursor = (const uint8_t *)data;
  while (bytes) {
    uint32_t available = SD_UPLOAD_RANGE_BLOCK_BYTES - s_sess.range_sha_bytes;
    size_t count = bytes < available ? bytes : available;
    if (mbedtls_sha256_update(&s_sess.range_sha, cursor, count) != 0) {
      return APP_ERR_STORAGE_WRITE;
    }
    s_sess.range_sha_bytes += (uint32_t)count;
    cursor += count;
    bytes -= count;
    if (s_sess.range_sha_bytes == SD_UPLOAD_RANGE_BLOCK_BYTES) {
      app_error_t result = range_sha_finish(true);
      if (result != APP_ERR_NONE) return result;
    }
  }
  return APP_ERR_NONE;
}

static app_error_t persist_pending_range_sha(void) {
  if (!s_sess.pending_sha_ready) return APP_ERR_NONE;
  char path[JSON_PATH_BYTES];
  snprintf(path, sizeof(path), "%s/%s", s_sess.dir,
           SD_UPLOAD_RANGE_HASH_FILE);
  FILE *file = fopen(path, "ab");
  if (!file) return errno_to_write_error();
  char line[112];
  int length = snprintf(line, sizeof(line), "%lu %lu %s\n",
                        (unsigned long)s_sess.pending_sha_offset,
                        (unsigned long)s_sess.pending_sha_length,
                        s_sess.pending_sha256);
  app_error_t result = APP_ERR_NONE;
  if (length <= 0 || length >= (int)sizeof(line) ||
      !file_write_all(file, line, (size_t)length)) {
    result = errno_to_write_error();
  }
  app_error_t close_result = file_sync_close(file);
  if (result == APP_ERR_NONE) result = close_result;
  if (result == APP_ERR_NONE) {
    ESP_LOGI(TAG,
             "LY|STORAGE_SHA|event=range_ready id=%s offset=%lu bytes=%lu",
             s_sess.id, (unsigned long)s_sess.pending_sha_offset,
             (unsigned long)s_sess.pending_sha_length);
    s_sess.pending_sha_ready = false;
  }
  return result;
}

static app_error_t wav_commit(FILE *file, uint32_t pcm_bytes) {
  uint8_t header[LUOYE_WAV_HEADER_BYTES];
  luoye_wav_build_header(header, AUDIO_SAMPLE_RATE, 1, 16, pcm_bytes);
  if (fseek(file, 0, SEEK_SET) != 0) return APP_ERR_STORAGE_WRITE;
  if (!file_write_all(file, header, sizeof(header))) return errno_to_write_error();
  app_error_t result = file_sync(file);
  if (result != APP_ERR_NONE) return result;
  if (fseek(file, 0, SEEK_END) != 0) return APP_ERR_STORAGE_WRITE;
  return APP_ERR_NONE;
}

static void json_set_string(cJSON *object, const char *name, const char *value) {
  cJSON_DeleteItemFromObjectCaseSensitive(object, name);
  cJSON_AddStringToObject(object, name, value ? value : "");
}

static void json_set_number(cJSON *object, const char *name, double value) {
  cJSON_DeleteItemFromObjectCaseSensitive(object, name);
  cJSON_AddNumberToObject(object, name, value);
}

static void json_set_bool(cJSON *object, const char *name, bool value) {
  cJSON_DeleteItemFromObjectCaseSensitive(object, name);
  cJSON_AddBoolToObject(object, name, value);
}

static app_error_t write_text_file(const char *path, const char *text) {
  FILE *file = fopen(path, "wb");
  if (!file) return errno_to_write_error();
  app_error_t result = APP_ERR_NONE;
  size_t bytes = strlen(text);
  if (!file_write_all(file, text, bytes)) result = errno_to_write_error();
  app_error_t close_result = file_sync_close(file);
  return result != APP_ERR_NONE ? result : close_result;
}

static app_error_t prepare_card_layout(void) {
  bool new_card = access(CARD_META_PATH, F_OK) != 0;
  if (!make_dir(SESSION_ROOT) || !make_dir(DIAG_ROOT)) {
    ESP_LOGE(TAG, "LY|STORAGE|event=layout_mkdir_failed errno=%d", errno);
    return errno_to_write_error();
  }

  /* Do a durable create/write/fsync/close/delete probe before recording is
     enabled.  A read-only, damaged or falsely mounted card fails at boot
     instead of losing the first recording. */
  app_error_t result = write_text_file(CARD_PROBE_PATH,
                                       "luoye-storage-write-test\n");
  if (result != APP_ERR_NONE) {
    unlink(CARD_PROBE_PATH);
    ESP_LOGE(TAG, "LY|STORAGE|event=write_probe_failed code=%d errno=%d",
             (int)result, errno);
    return result;
  }
  if (unlink(CARD_PROBE_PATH) != 0 && errno != ENOENT) {
    ESP_LOGE(TAG, "LY|STORAGE|event=write_probe_cleanup_failed errno=%d",
             errno);
    return APP_ERR_STORAGE_WRITE;
  }

  if (new_card) {
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char manifest[384];
    snprintf(manifest, sizeof(manifest),
        "{\"schema\":\"%s\",\"card_uuid\":\"%02X%02X%02X%02X%02X%02X-%08lX\","
        "\"filesystem\":\"fat32\",\"sector_bytes\":512,\"cluster_bytes\":32768,"
        "\"directories\":[\"rec\",\"diag\",\"system\"]}\n",
        CARD_SCHEMA, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
        (unsigned long)esp_random());
    if (!make_dir(MOUNT_POINT "/system")) return errno_to_write_error();
    result = write_text_file(CARD_META_PATH, manifest);
    if (result != APP_ERR_NONE) {
      ESP_LOGE(TAG, "LY|STORAGE|event=card_manifest_failed code=%d",
               (int)result);
      return result;
    }
  }
  ESP_LOGI(TAG, "LY|STORAGE|event=layout_ready new_card=%d rec=1 diag=1",
           new_card);
  return APP_ERR_NONE;
}

static bool card_schema_current(void) {
  FILE *file = fopen(CARD_META_PATH, "rb");
  if (!file) return false;
  char text[512] = {0};
  size_t read = fread(text, 1, sizeof(text) - 1, file);
  fclose(file);
  if (!read) return false;
  cJSON *root = cJSON_Parse(text);
  cJSON *schema = root ? cJSON_GetObjectItemCaseSensitive(root, "schema") : NULL;
  bool current = cJSON_IsString(schema) &&
                 strcmp(schema->valuestring, CARD_SCHEMA) == 0;
  cJSON_Delete(root);
  return current;
}

static app_error_t verify_probe_range(FILE *file, uint8_t *buffer,
                                      size_t buffer_size, uint32_t offset,
                                      uint32_t length) {
  if (storage_sd_seek(file, offset) != ESP_OK) return APP_ERR_STORAGE_WRITE;
  uint32_t done = 0;
  while (done < length) {
    size_t wanted = length - done < buffer_size ? length - done : buffer_size;
    size_t received = 0;
    if (storage_sd_read(file, buffer, wanted, &received) != ESP_OK ||
        received != wanted) {
      return APP_ERR_STORAGE_WRITE;
    }
    for (size_t i = 0; i < wanted; ++i) {
      uint8_t expected =
          (uint8_t)((((uint32_t)offset + done + i) * 131U + 0x5AU) & 0xffU);
      if (buffer[i] != expected) return APP_ERR_STORAGE_WRITE;
    }
    done += (uint32_t)wanted;
  }
  ESP_LOGI(TAG, "LY|STORAGE_SELFTEST|offset=%lu bytes=%lu result=ok",
           (unsigned long)offset, (unsigned long)length);
  return APP_ERR_NONE;
}

static app_error_t sd_speed_probe(void) {
  enum { PROBE_BLOCK_BYTES = 8192, PROBE_TOTAL_BYTES = 128 * 1024 };
  uint8_t *buffer = malloc(PROBE_BLOCK_BYTES);
  if (!buffer) {
    ESP_LOGW(TAG,
             "LY|STORAGE_PERF|event=probe_skipped reason=no_memory freq_khz=%lu",
             (unsigned long)s_sd_freq_khz);
    /* Lack of diagnostic memory must not make a proven-safe 20 MHz card
       unusable. The normal recording path remains independently checked. */
    return APP_ERR_NONE;
  }
  unlink(CARD_SPEED_PROBE_PATH);
  FILE *file = fopen(CARD_SPEED_PROBE_PATH, "wb");
  if (!file) {
    free(buffer);
    return errno_to_write_error();
  }
  app_error_t result = APP_ERR_NONE;
  int64_t write_started_us = esp_timer_get_time();
  for (uint32_t offset = 0; offset < PROBE_TOTAL_BYTES; offset += PROBE_BLOCK_BYTES) {
    for (uint32_t i = 0; i < PROBE_BLOCK_BYTES; ++i) {
      buffer[i] = (uint8_t)(((offset + i) * 131U + 0x5AU) & 0xffU);
    }
    if (!file_write_all(file, buffer, PROBE_BLOCK_BYTES)) {
      result = errno_to_write_error();
      break;
    }
  }
  app_error_t close_result = file_sync_close(file);
  if (result == APP_ERR_NONE) result = close_result;
  uint64_t write_us = (uint64_t)(esp_timer_get_time() - write_started_us);

  uint64_t read_us = 0;
  if (result == APP_ERR_NONE) {
    file = fopen(CARD_SPEED_PROBE_PATH, "rb");
    if (!file) {
      result = APP_ERR_STORAGE_OPEN;
    } else {
      int64_t read_started_us = esp_timer_get_time();
      for (uint32_t offset = 0; offset < PROBE_TOTAL_BYTES;
           offset += PROBE_BLOCK_BYTES) {
        size_t received = 0;
        if (storage_sd_read(file, buffer, PROBE_BLOCK_BYTES, &received) != ESP_OK ||
            received != PROBE_BLOCK_BYTES) {
          result = APP_ERR_STORAGE_WRITE;
          break;
        }
        for (uint32_t i = 0; i < PROBE_BLOCK_BYTES; ++i) {
          uint8_t expected =
              (uint8_t)(((offset + i) * 131U + 0x5AU) & 0xffU);
          if (buffer[i] != expected) {
            result = APP_ERR_STORAGE_WRITE;
            break;
          }
        }
        if (result != APP_ERR_NONE) break;
      }
      static const struct { uint32_t offset; uint32_t length; } cases[] = {
        {0, 512}, {1, 509}, {44, 511}, {511, 516},
        {512, 16 * 1024}, {4096, 64 * 1024},
      };
      for (size_t i = 0; result == APP_ERR_NONE &&
                         i < sizeof(cases) / sizeof(cases[0]); ++i) {
        result = verify_probe_range(file, buffer, PROBE_BLOCK_BYTES,
                                    cases[i].offset, cases[i].length);
      }
      read_us = (uint64_t)(esp_timer_get_time() - read_started_us);
      fclose(file);
    }
  }
  unlink(CARD_SPEED_PROBE_PATH);
  free(buffer);
  ESP_LOGI(TAG,
           "LY|STORAGE_PERF|event=probe result=%d freq_khz=%lu bytes=%u write_Bps=%llu read_Bps=%llu",
           (int)result, (unsigned long)s_sd_freq_khz, PROBE_TOTAL_BYTES,
           write_us ? (unsigned long long)(PROBE_TOTAL_BYTES * 1000000ULL / write_us) : 0,
           read_us ? (unsigned long long)(PROBE_TOTAL_BYTES * 1000000ULL / read_us) : 0);
  return result;
}

static app_error_t write_json_atomic(const char *path, cJSON *root) {
  char *json = cJSON_PrintUnformatted(root);
  if (!json) return APP_ERR_STORAGE_WRITE;
  cJSON *validation = cJSON_Parse(json);
  if (!validation) {
    cJSON_free(json);
    return APP_ERR_STORAGE_WRITE;
  }
  cJSON_Delete(validation);

  char temp[JSON_PATH_BYTES], backup[JSON_PATH_BYTES];
  if (snprintf(temp, sizeof(temp), "%s.tmp", path) >= (int)sizeof(temp) ||
      snprintf(backup, sizeof(backup), "%s.bak", path) >= (int)sizeof(backup)) {
    cJSON_free(json);
    return APP_ERR_STORAGE_OPEN;
  }
  app_error_t result = write_text_file(temp, json);
  cJSON_free(json);
  if (result != APP_ERR_NONE) {
    unlink(temp);
    return result;
  }

  unlink(backup);
  bool had_current = access(path, F_OK) == 0;
  if (had_current && rename(path, backup) != 0) {
    unlink(temp);
    return APP_ERR_STORAGE_WRITE;
  }
  if (rename(temp, path) != 0) {
    if (had_current) rename(backup, path);
    unlink(temp);
    return APP_ERR_STORAGE_WRITE;
  }
  return APP_ERR_NONE;
}

static cJSON *read_json(const char *path) {
  FILE *file = fopen(path, "rb");
  if (!file) return NULL;
  size_t length = 0;
  if (storage_sd_size(file, &length) != ESP_OK || length > 64 * 1024U ||
      storage_sd_seek(file, 0) != ESP_OK) {
    fclose(file);
    return NULL;
  }
  char *buffer = malloc(length + 1U);
  if (!buffer) {
    fclose(file);
    return NULL;
  }
  size_t read = 0;
  esp_err_t read_result = storage_sd_read(file, buffer, length, &read);
  fclose(file);
  if (read_result != ESP_OK || read != length) {
    free(buffer);
    return NULL;
  }
  buffer[length] = '\0';
  cJSON *root = cJSON_Parse(buffer);
  free(buffer);
  return root;
}

static cJSON *new_manifest(const char *session_id,
                           app_scene_t scene,
                           const char *title) {
  cJSON *root = cJSON_CreateObject();
  if (!root) return NULL;
  cJSON_AddStringToObject(root, "schema", "luoye-session/1-draft");
  cJSON_AddStringToObject(root, "client_session_id", session_id);
  cJSON_AddNullToObject(root, "server_session_id");
  cJSON_AddStringToObject(root, "state", "local_recording");
  cJSON_AddStringToObject(root, "scene",
                         scene == APP_SCENE_TRANSLATE ? "translate" : "meeting");
  cJSON_AddStringToObject(root, "title", title ? title : "");
  time_t started = time(NULL);
  if (started >= 1577836800) {
    cJSON_AddNumberToObject(root, "started_at_utc", (double)started);
  } else {
    cJSON_AddNullToObject(root, "started_at_utc");
  }
  cJSON_AddNullToObject(root, "ended_at_utc");

  cJSON *audio = cJSON_AddObjectToObject(root, "audio");
  cJSON_AddStringToObject(audio, "path", "audio.wav");
  cJSON_AddStringToObject(audio, "codec", "pcm_s16le");
  cJSON_AddNumberToObject(audio, "sample_rate", AUDIO_SAMPLE_RATE);
  cJSON_AddNumberToObject(audio, "channels", 1);
  cJSON_AddStringToObject(audio, "range_sha256_path",
                         SD_UPLOAD_RANGE_HASH_FILE);
  cJSON_AddNumberToObject(audio, "range_sha256_bytes",
                          SD_UPLOAD_RANGE_BLOCK_BYTES);
  cJSON_AddNumberToObject(audio, "pcm_bytes_committed", 0);
  cJSON_AddBoolToObject(audio, "wav_closed", false);
  cJSON_AddNumberToObject(audio, "captured_samples", 0);
  cJSON_AddNumberToObject(audio, "dropped_samples", 0);
  cJSON_AddNumberToObject(audio, "overflow_events", 0);

  cJSON *marks = cJSON_AddObjectToObject(root, "marks");
  cJSON_AddStringToObject(marks, "path", "marks.jsonl");
  cJSON_AddNumberToObject(marks, "count", 0);

  cJSON *recovery = cJSON_AddObjectToObject(root, "recovery");
  cJSON_AddStringToObject(recovery, "close_reason", "recording");
  cJSON_AddNumberToObject(recovery, "repair_count", 0);
  return root;
}

static app_error_t write_session_manifest(const char *state,
                                          app_close_reason_t close_reason,
                                          app_error_t session_error) {
  char path[JSON_PATH_BYTES];
  snprintf(path, sizeof(path), "%s/session.json", s_sess.dir);
  cJSON *root = read_json(path);
  if (!root) root = new_manifest(s_sess.id, s_sess.scene, s_sess.title);
  if (!root) return APP_ERR_STORAGE_WRITE;

  json_set_string(root, "state", state);
  if (strcmp(state, "local_closed") == 0) {
    time_t ended = time(NULL);
    cJSON_DeleteItemFromObjectCaseSensitive(root, "ended_at_utc");
    if (ended >= 1577836800) {
      cJSON_AddNumberToObject(root, "ended_at_utc", (double)ended);
    } else {
      cJSON_AddNullToObject(root, "ended_at_utc");
    }
  }
  cJSON *audio = cJSON_GetObjectItemCaseSensitive(root, "audio");
  if (!cJSON_IsObject(audio)) {
    cJSON_DeleteItemFromObjectCaseSensitive(root, "audio");
    audio = cJSON_AddObjectToObject(root, "audio");
  }
  audio_pdm_stream_stats_t stats = {0};
  audio_pdm_get_stream_stats(&stats);
  json_set_number(audio, "pcm_bytes_committed", s_sess.committed_bytes);
  json_set_bool(audio, "wav_closed", strcmp(state, "local_closed") == 0);
  json_set_number(audio, "captured_samples", (double)stats.captured_samples);
  json_set_number(audio, "dropped_samples", (double)stats.dropped_samples);
  json_set_number(audio, "overflow_events", stats.overflow_events);

  cJSON *recovery = cJSON_GetObjectItemCaseSensitive(root, "recovery");
  if (!cJSON_IsObject(recovery)) {
    cJSON_DeleteItemFromObjectCaseSensitive(root, "recovery");
    recovery = cJSON_AddObjectToObject(root, "recovery");
  }
  const char *reason = close_reason == APP_CLOSE_LOW_BATTERY ? "low_battery" :
                       close_reason == APP_CLOSE_STORAGE_ERROR ? "storage_error" :
                       "user";
  json_set_string(recovery, "close_reason", reason);
  if (session_error != APP_ERR_NONE) {
    json_set_number(recovery, "error_code", session_error);
  }
  app_error_t result = write_json_atomic(path, root);
  cJSON_Delete(root);
  return result;
}

static void post_storage_error(app_error_t error, const char *phase) {
  if (error == APP_ERR_NONE) error = APP_ERR_STORAGE_WRITE;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  s_sess.error = error;
  s_sess.close_req = true;
  bool should_post = !s_sess.error_posted;
  s_sess.error_posted = true;
  xSemaphoreGive(s_lock);
  ESP_LOGE(TAG, "LY|STORAGE|event=error phase=%s code=%d errno=%d",
           phase, (int)error, errno);
  if (should_post && s_post) s_post(APP_EV_STORAGE_ERROR, error);
}

esp_err_t sd_session_generate_id(char *out, size_t out_size) {
  if (!out || out_size < SESSION_ID_BYTES) return ESP_ERR_INVALID_ARG;
  nvs_handle_t nvs;
  esp_err_t error = nvs_open("luoye_store", NVS_READWRITE, &nvs);
  if (error != ESP_OK) return error;
  uint64_t counter = 0;
  error = nvs_get_u64(nvs, "session_seq", &counter);
  if (error == ESP_ERR_NVS_NOT_FOUND) {
    counter = 0;
    error = ESP_OK;
  }
  if (error == ESP_OK) error = nvs_set_u64(nvs, "session_seq", counter + 1);
  if (error == ESP_OK) error = nvs_commit(nvs);
  nvs_close(nvs);
  if (error != ESP_OK) return error;

  uint8_t mac[6] = {0};
  error = esp_read_mac(mac, ESP_MAC_WIFI_STA);
  if (error != ESP_OK) return error;
  uint64_t mac_value = 0;
  for (size_t i = 0; i < sizeof(mac); ++i) {
    mac_value = (mac_value << 8) | mac[i];
  }
  snprintf(out, out_size, "LY-%012llX-%010llu-%08lX",
           (unsigned long long)mac_value,
           (unsigned long long)(counter + 1),
           (unsigned long)esp_random());
  return ESP_OK;
}

app_error_t sd_session_open(const char *session_id,
                            app_scene_t scene,
                            const char *title) {
  if (!s_card) return APP_ERR_NO_SD;
  if (s_recovery_blocked) return APP_ERR_RECOVERY;
  if (!session_id || !*session_id || s_sess.open) return APP_ERR_BUSY;

  memset(&s_sess, 0, sizeof(s_sess));
  snprintf(s_sess.dir, sizeof(s_sess.dir), SESSION_ROOT "/%s", session_id);
  strncpy(s_sess.id, session_id, sizeof(s_sess.id) - 1);
  strncpy(s_sess.title, title ? title : "", sizeof(s_sess.title) - 1);
  s_sess.scene = scene;
  s_sess.close_reason = APP_CLOSE_USER;

  if (!make_dir(SESSION_ROOT) || !make_dir(s_sess.dir)) {
    return errno_to_write_error();
  }

  char path[JSON_PATH_BYTES];
  snprintf(path, sizeof(path), "%s/audio.wav", s_sess.dir);
  s_sess.wav = fopen(path, "wb");
  if (!s_sess.wav) return errno_to_write_error();
  app_error_t result = wav_commit(s_sess.wav, 0);
  if (result != APP_ERR_NONE) {
    fclose(s_sess.wav);
    s_sess.wav = NULL;
    return result;
  }

  snprintf(path, sizeof(path), "%s/marks.jsonl", s_sess.dir);
  result = write_text_file(path, "");
  if (result == APP_ERR_NONE) {
    snprintf(path, sizeof(path), "%s/%s", s_sess.dir,
             SD_UPLOAD_RANGE_HASH_FILE);
    result = write_text_file(path, "");
  }
  if (result == APP_ERR_NONE) {
    snprintf(path, sizeof(path), "%s/upload.state", s_sess.dir);
    result = write_text_file(path,
        "{\"schema\":\"luoye-upload/2\","
        "\"client_session_id\":\"\",\"device_id\":\"\","
        "\"binding_generation\":0,\"state\":\"queued\","
        "\"remote_session_created\":false,\"server_session_id\":\"\","
        "\"next_seq\":0,\"acked_pcm_bytes\":0,\"marks_acked\":false,"
        "\"final_acked\":false,\"retry_count\":0,\"last_http_status\":0,"
        "\"result_revision\":0,\"result_pcm_bytes\":0}\n");
  }
  if (result == APP_ERR_NONE) {
    cJSON *manifest = new_manifest(s_sess.id, scene, title);
    if (!manifest) result = APP_ERR_STORAGE_WRITE;
    else {
      snprintf(path, sizeof(path), "%s/session.json", s_sess.dir);
      result = write_json_atomic(path, manifest);
      cJSON_Delete(manifest);
    }
  }
  if (result == APP_ERR_NONE) {
    app_error_t hash_result = range_sha_start();
    if (hash_result != APP_ERR_NONE) {
      range_sha_disable("start", hash_result);
    }
  }
  if (result != APP_ERR_NONE) {
    file_sync_close(s_sess.wav);
    s_sess.wav = NULL;
    return result;
  }

  s_sess.open = true;
  ESP_LOGI(TAG, "LY|STORAGE|event=session_open id=%s durable_files=5 sha_range=%u",
           s_sess.id, (unsigned)SD_UPLOAD_RANGE_BLOCK_BYTES);
  return APP_ERR_NONE;
}

void sd_session_request_close(app_close_reason_t reason) {
  if (!s_lock) return;
  xSemaphoreTake(s_lock, portMAX_DELAY);
  if (s_sess.open) {
    s_sess.close_reason = reason;
    s_sess.close_req = true;
  }
  xSemaphoreGive(s_lock);
}

bool sd_session_is_open(void) { return s_sess.open; }

app_error_t sd_session_close_status(void) {
  bool open_now = __atomic_load_n(&s_sess.open, __ATOMIC_ACQUIRE);
  bool closed_now = __atomic_load_n(&s_sess.closed, __ATOMIC_ACQUIRE);
  if (open_now || !closed_now) return APP_ERR_BUSY;
  return s_sess.error;
}

esp_err_t sd_session_mark(const char *kind, int64_t at_ms) {
  if (!s_sess.open) return ESP_ERR_INVALID_STATE;
  char path[JSON_PATH_BYTES], line[128];
  snprintf(path, sizeof(path), "%s/marks.jsonl", s_sess.dir);
  int length = snprintf(line, sizeof(line),
                        "{\"kind\":\"%s\",\"at_ms\":%lld}\n",
                        kind ? kind : "unknown", (long long)at_ms);
  if (length <= 0 || length >= (int)sizeof(line)) return ESP_ERR_INVALID_SIZE;
  FILE *file = fopen(path, "ab");
  if (!file) {
    post_storage_error(errno_to_write_error(), "mark_open");
    return ESP_FAIL;
  }
  app_error_t result = APP_ERR_NONE;
  if (!file_write_all(file, line, (size_t)length)) result = errno_to_write_error();
  app_error_t close_result = file_sync_close(file);
  if (result == APP_ERR_NONE) result = close_result;
  if (result != APP_ERR_NONE) {
    post_storage_error(result, "mark_sync");
    return ESP_FAIL;
  }
  return ESP_OK;
}

bool sd_session_current(char *dir_out, size_t dir_len,
                        char *id_out, size_t id_len,
                        bool *closed, uint32_t *data_bytes) {
  bool open_now = __atomic_load_n(&s_sess.open, __ATOMIC_ACQUIRE);
  bool closed_now = __atomic_load_n(&s_sess.closed, __ATOMIC_ACQUIRE);
  if (!open_now && !closed_now) return false;
  if (dir_out && dir_len) {
    strncpy(dir_out, s_sess.dir, dir_len - 1);
    dir_out[dir_len - 1] = '\0';
  }
  if (id_out && id_len) {
    strncpy(id_out, s_sess.id, id_len - 1);
    id_out[id_len - 1] = '\0';
  }
  if (closed) *closed = closed_now;
  if (data_bytes) {
    *data_bytes = __atomic_load_n(&s_sess.committed_bytes, __ATOMIC_ACQUIRE);
  }
  return true;
}

static void finish_session(void) {
  app_error_t hash_result = range_sha_finish(false);
  if (hash_result != APP_ERR_NONE) {
    range_sha_disable("finish", hash_result);
  }
  app_error_t result = wav_commit(s_sess.wav, s_sess.data_bytes);
  if (result == APP_ERR_NONE) {
    __atomic_store_n(&s_sess.committed_bytes, s_sess.data_bytes, __ATOMIC_RELEASE);
  }
  if (result == APP_ERR_NONE && s_sess.pending_sha_ready) {
    hash_result = persist_pending_range_sha();
    if (hash_result != APP_ERR_NONE) {
      range_sha_disable("close_sync", hash_result);
    }
  }
  if (s_sess.range_sha_active) {
    mbedtls_sha256_free(&s_sess.range_sha);
    s_sess.range_sha_active = false;
  }
  app_error_t close_result = file_sync_close(s_sess.wav);
  s_sess.wav = NULL;
  if (result == APP_ERR_NONE) result = close_result;

  if (s_sess.error != APP_ERR_NONE && result == APP_ERR_NONE) {
    result = s_sess.error;
  }
  const bool success = result == APP_ERR_NONE;
  app_error_t manifest_result = write_session_manifest(
      success ? "local_closed" : "local_error",
      s_sess.close_reason, result);
  if (result == APP_ERR_NONE) result = manifest_result;

  if (result != APP_ERR_NONE) s_sess.error = result;
  __atomic_store_n(&s_sess.open, false, __ATOMIC_RELEASE);
  __atomic_store_n(&s_sess.closed, true, __ATOMIC_RELEASE);
  if (result == APP_ERR_NONE) {
    ESP_LOGI(TAG,
             "LY|STORAGE|event=session_close_done id=%s pcm=%u reason=%d",
             s_sess.id, (unsigned)s_sess.committed_bytes,
             (int)s_sess.close_reason);
    if (s_post) s_post(APP_EV_SESSION_CLOSE_DONE, 0);
  } else {
    post_storage_error(result, "close");
    if (s_post) s_post(APP_EV_SESSION_SETTLED, 0);
  }
}

static void writer_task(void *arg) {
  (void)arg;
  static int16_t buffer[WRITE_SAMPLES];
  uint32_t since_sync = 0;
  for (;;) {
    if (!s_sess.open) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    size_t samples = audio_pdm_read(buffer, WRITE_SAMPLES, 100);
    if (samples > 0 && s_sess.error == APP_ERR_NONE) {
      size_t written = fwrite(buffer, sizeof(int16_t), samples, s_sess.wav);
      size_t written_bytes = written * sizeof(int16_t);
      s_sess.data_bytes += (uint32_t)written_bytes;
      since_sync += (uint32_t)written_bytes;
      if (written != samples) {
        post_storage_error(errno_to_write_error(), "pcm_short_write");
      } else {
        app_error_t hash_result = range_sha_feed(buffer, written_bytes);
        if (hash_result != APP_ERR_NONE) {
          range_sha_disable("update", hash_result);
        }
        if (fflush(s_sess.wav) != 0) {
          post_storage_error(errno_to_write_error(), "pcm_flush");
        } else if (since_sync >= SYNC_INTERVAL_BYTES) {
          app_error_t result = wav_commit(s_sess.wav, s_sess.data_bytes);
          if (result != APP_ERR_NONE) post_storage_error(result, "periodic_sync");
          else {
            __atomic_store_n(&s_sess.committed_bytes, s_sess.data_bytes,
                             __ATOMIC_RELEASE);
            result = persist_pending_range_sha();
            if (result != APP_ERR_NONE) {
              range_sha_disable("periodic_sync", result);
            }
            since_sync = 0;
          }
        }
      }
    }

    if (s_sess.close_req && samples == 0) {
      finish_session();
      since_sync = 0;
    }
  }
}

static app_error_t repair_wav(const char *path, uint32_t *pcm_bytes) {
  FILE *file = fopen(path, "r+b");
  if (!file) return APP_ERR_RECOVERY;
  if (fseek(file, 0, SEEK_END) != 0) {
    fclose(file);
    return APP_ERR_RECOVERY;
  }
  long size = ftell(file);
  if (size < 0) {
    fclose(file);
    return APP_ERR_RECOVERY;
  }
  luoye_wav_repair_plan_t plan = luoye_wav_plan_repair((uint64_t)size);
  int fd = fileno(file);
  if (fd < 0 || (plan.needs_truncate &&
      ftruncate(fd, (off_t)plan.repaired_size) != 0)) {
    fclose(file);
    return APP_ERR_RECOVERY;
  }
  app_error_t result = wav_commit(file, plan.pcm_bytes);
  app_error_t close_result = file_sync_close(file);
  if (result == APP_ERR_NONE) result = close_result;
  if (pcm_bytes) *pcm_bytes = plan.pcm_bytes;
  return result;
}

static app_error_t repair_jsonl_tail(const char *path) {
  FILE *file = fopen(path, "r+b");
  if (!file) {
    if (errno == ENOENT) return write_text_file(path, "");
    return APP_ERR_RECOVERY;
  }
  if (fseek(file, 0, SEEK_END) != 0) {
    fclose(file);
    return APP_ERR_RECOVERY;
  }
  long size = ftell(file);
  long keep = 0;
  for (long pos = size; pos > 0; --pos) {
    if (fseek(file, pos - 1, SEEK_SET) != 0) break;
    int ch = fgetc(file);
    if (ch == '\n') {
      keep = pos;
      break;
    }
  }
  int fd = fileno(file);
  app_error_t result = APP_ERR_NONE;
  if (fd < 0 || ftruncate(fd, keep) != 0) result = APP_ERR_RECOVERY;
  app_error_t close_result = file_sync_close(file);
  return result != APP_ERR_NONE ? result : close_result;
}

static app_error_t recover_session(const char *dir, const char *id) {
  char manifest_path[JSON_PATH_BYTES], wav_path[JSON_PATH_BYTES];
  char marks_path[JSON_PATH_BYTES], upload_path[JSON_PATH_BYTES];
  snprintf(manifest_path, sizeof(manifest_path), "%s/session.json", dir);
  snprintf(wav_path, sizeof(wav_path), "%s/audio.wav", dir);
  snprintf(marks_path, sizeof(marks_path), "%s/marks.jsonl", dir);
  snprintf(upload_path, sizeof(upload_path), "%s/upload.state", dir);

  cJSON *root = read_json(manifest_path);
  cJSON *state = root ? cJSON_GetObjectItemCaseSensitive(root, "state") : NULL;
  if (cJSON_IsString(state)) {
    const char *value = state->valuestring;
    if (strcmp(value, "local_closed") == 0 || strcmp(value, "recovered") == 0) {
      cJSON_Delete(root);
      return APP_ERR_NONE;
    }
  }
  if (!root) root = new_manifest(id, APP_SCENE_MEETING, "");
  if (!root) return APP_ERR_RECOVERY;

  uint32_t pcm_bytes = 0;
  app_error_t result = repair_wav(wav_path, &pcm_bytes);
  if (result == APP_ERR_NONE) result = repair_jsonl_tail(marks_path);
  if (result == APP_ERR_NONE && access(upload_path, F_OK) != 0) {
    result = write_text_file(upload_path,
        "{\"schema\":\"luoye-upload/2\",\"state\":\"queued\","
        "\"acked_pcm_bytes\":0,\"result_revision\":0,"
        "\"result_pcm_bytes\":0}\n");
  }
  if (result != APP_ERR_NONE) {
    cJSON_Delete(root);
    return result;
  }

  json_set_string(root, "state", "recovered");
  cJSON *audio = cJSON_GetObjectItemCaseSensitive(root, "audio");
  if (!cJSON_IsObject(audio)) {
    cJSON_DeleteItemFromObjectCaseSensitive(root, "audio");
    audio = cJSON_AddObjectToObject(root, "audio");
  }
  json_set_number(audio, "pcm_bytes_committed", pcm_bytes);
  json_set_bool(audio, "wav_closed", true);
  cJSON *recovery = cJSON_GetObjectItemCaseSensitive(root, "recovery");
  if (!cJSON_IsObject(recovery)) {
    cJSON_DeleteItemFromObjectCaseSensitive(root, "recovery");
    recovery = cJSON_AddObjectToObject(root, "recovery");
  }
  cJSON *count = cJSON_GetObjectItemCaseSensitive(recovery, "repair_count");
  json_set_number(recovery, "repair_count",
                  cJSON_IsNumber(count) ? count->valuedouble + 1 : 1);
  json_set_string(recovery, "close_reason", "power_loss");

  result = write_json_atomic(manifest_path, root);
  cJSON_Delete(root);
  if (result == APP_ERR_NONE) {
    ESP_LOGW(TAG, "LY|STORAGE|event=recovered id=%s pcm=%u",
             id, (unsigned)pcm_bytes);
  }
  return result;
}

static app_error_t recover_incomplete_sessions(void) {
  if (!make_dir(SESSION_ROOT)) return APP_ERR_RECOVERY;
  DIR *root = opendir(SESSION_ROOT);
  if (!root) return APP_ERR_RECOVERY;
  app_error_t result = APP_ERR_NONE;
  struct dirent *entry;
  while ((entry = readdir(root)) != NULL) {
    if (entry->d_name[0] == '.') continue;
    char dir[SESSION_DIR_BYTES], wav[JSON_PATH_BYTES];
    if (strlen(entry->d_name) >= SESSION_ID_BYTES) continue;
    snprintf(dir, sizeof(dir), SESSION_ROOT "/%.*s",
             SESSION_ID_BYTES - 1, entry->d_name);
    struct stat st;
    if (stat(dir, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
    snprintf(wav, sizeof(wav), "%s/audio.wav", dir);
    if (access(wav, F_OK) != 0) continue;
    app_error_t one = recover_session(dir, entry->d_name);
    if (one != APP_ERR_NONE) {
      ESP_LOGE(TAG, "LY|STORAGE|event=recovery_failed id=%s code=%d",
               entry->d_name, (int)one);
      result = one;
    }
  }
  closedir(root);
  return result;
}

static void space_task(void *arg) {
  (void)arg;
  for (;;) {
    uint64_t total = 0, free_bytes = 0;
    if (esp_vfs_fat_info(MOUNT_POINT, &total, &free_bytes) == ESP_OK &&
        total > 0 && s_post) {
      s_post(APP_EV_SD_LOW, free_bytes < total / 10);
    }
    vTaskDelay(pdMS_TO_TICKS(60 * 1000));
  }
}

static void power_diag_task(void *arg) {
  (void)arg;
  uint32_t last_sequence = 0;
  for (;;) {
    power_diag_sample_t value;
    if (power_diag_snapshot(&value) && value.sequence != last_sequence) {
      last_sequence = value.sequence;
      xSemaphoreTake(s_power_diag_lock, portMAX_DELAY);
      bool new_file = access(POWER_DIAG_PATH, F_OK) != 0;
      FILE *file = fopen(POWER_DIAG_PATH, "ab");
      if (file) {
        if (new_file) {
          fputs("epoch_utc,uptime_s,sequence,recording,gauge_soc,filtered_soc,displayed_soc,mv,usb,charge_state,bq_ok,bq_phase,ichg_ma,ilim_ma,stat0,stat1,ichg_ctrl,tmr_ilim,sys_reg,max_config,max_hibrt,max_status,max_version,voltage_fallback\n", file);
        }
        time_t now = time(NULL);
        unsigned long uptime_s = (unsigned long)(xTaskGetTickCount() /
                                                  configTICK_RATE_HZ);
        fprintf(file,
                "%lld,%lu,%lu,%d,%d.%02d,%d,%d,%d,%d,%d,%d,%u,%d,%d,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X%02X,0x%02X%02X,0x%02X%02X,0x%02X%02X,%d\n",
                (long long)now, uptime_s, (unsigned long)value.sequence,
                sd_session_is_open(),
                value.gauge_soc_x256 < 0 ? -1 :
                  (int)(value.gauge_soc_x256 / 256),
                value.gauge_soc_x256 < 0 ? 0 :
                  (int)((value.gauge_soc_x256 % 256) * 100 / 256),
                value.filtered_soc, value.displayed_soc, value.battery_mv,
                value.usb_present, (int)value.charge_state, value.bq_ok,
                (unsigned)((value.bq_stat0 >> 5) & 0x03U), value.charge_ma,
                value.input_limit_ma, value.bq_stat0, value.bq_stat1,
                value.bq_ichg_ctrl, value.bq_tmr_ilim, value.bq_sys_reg,
                value.max_config[0], value.max_config[1],
                value.max_hibrt[0], value.max_hibrt[1],
                value.max_status[0], value.max_status[1],
                value.max_version[0], value.max_version[1],
                value.voltage_fallback);
        if (fflush(file) != 0) {
          ESP_LOGW(TAG, "LY|POWER_DIAG|event=flush_failed errno=%d", errno);
        }
        fclose(file);
      } else {
        ESP_LOGW(TAG, "LY|POWER_DIAG|event=open_failed errno=%d", errno);
      }
      xSemaphoreGive(s_power_diag_lock);
    }
    vTaskDelay(pdMS_TO_TICKS(60 * 1000));
  }
}

esp_err_t storage_sd_init(storage_post_fn post) {
  s_post = post;
  s_lock = xSemaphoreCreateMutex();
  s_power_diag_lock = xSemaphoreCreateMutex();
  s_dma_read_lock = xSemaphoreCreateMutex();
  if (!s_lock || !s_power_diag_lock || !s_dma_read_lock) {
    ESP_LOGE(TAG,
             "LY|STORAGE_DMA|event=policy_failed low_water=%u free_internal=%lu largest_dma=%lu",
             (unsigned)SD_DMA_LOW_WATER_BYTES,
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_DMA));
    return ESP_ERR_NO_MEM;
  }
  ESP_LOGI(TAG,
           "LY|STORAGE_DMA|event=policy mode=direct low_water=%u timeout_ms=%u free_internal=%lu largest_dma=%lu",
           (unsigned)SD_DMA_LOW_WATER_BYTES,
           (unsigned)SD_DMA_LOW_WATER_TIMEOUT_MS,
           (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_DMA));

  s_storage_ready = false;
  esp_err_t error = sd_mount_with_retry(false);
  if (error != ESP_OK) {
    ESP_LOGE(TAG, "LY|STORAGE|event=mount_failed attempts=%u esp=%s",
             SD_MOUNT_ATTEMPTS, esp_err_to_name(error));
    return error;
  }
  ESP_LOGI(TAG, "LY|STORAGE|event=mounted size_mb=%llu",
           ((uint64_t)s_card->csd.capacity * s_card->csd.sector_size) >> 20);

  if (s_card->csd.sector_size != 512 || !card_schema_current()) {
    ESP_LOGW(TAG,
             "LY|STORAGE|event=initialization_required schema=%s sector=%u action=long_press_record",
             CARD_SCHEMA, (unsigned)s_card->csd.sector_size);
    if (s_post) s_post(APP_EV_STORAGE_ERROR, APP_ERR_STORAGE_FORMAT_REQUIRED);
    return ESP_ERR_INVALID_VERSION;
  }

  app_error_t layout = prepare_card_layout();
  app_error_t speed = layout == APP_ERR_NONE ? sd_speed_probe() : layout;
  if (layout != APP_ERR_NONE || speed != APP_ERR_NONE) {
    return ESP_ERR_INVALID_STATE;
  }

  app_error_t recovery = recover_incomplete_sessions();
  s_recovery_blocked = recovery != APP_ERR_NONE;
  if (s_recovery_blocked) {
    ESP_LOGE(TAG, "LY|STORAGE|event=recovery_blocked code=%d", (int)recovery);
    return ESP_ERR_INVALID_STATE;
  }
  error = sd_upload_store_init();
  if (error != ESP_OK) return error;
  if (xTaskCreatePinnedToCore(writer_task, "sd_writer", 6144, NULL, 15,
                              NULL, 0) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  if (xTaskCreate(space_task, "sd_space", 3072, NULL, 3, NULL) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  if (xTaskCreate(power_diag_task, "power_diag", 4096, NULL, 2, NULL) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  usb_serial_jtag_driver_config_t usb_config =
      USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
  esp_err_t usb_error = usb_serial_jtag_driver_install(&usb_config);
  if (usb_error == ESP_OK || usb_error == ESP_ERR_INVALID_STATE) {
    usb_serial_jtag_vfs_use_driver();
    if (xTaskCreate(usb_command_task, "usb_sd_export", 6144, NULL, 4,
                    NULL) != pdPASS) {
      ESP_LOGW(TAG, "LY|SD_EXPORT|event=task_create_failed");
    }
  } else {
    ESP_LOGW(TAG, "LY|SD_EXPORT|event=usb_driver_failed esp=%s",
             esp_err_to_name(usb_error));
  }
  s_storage_ready = true;
  return ESP_OK;
}

bool storage_sd_mounted(void) {
  return s_card != NULL && s_storage_ready && !s_recovery_blocked;
}

esp_err_t storage_sd_format(void) {
  if (s_format_in_progress || sd_session_is_open()) return ESP_ERR_INVALID_STATE;
  s_format_in_progress = true;
  s_storage_ready = false;
  esp_err_t error = ESP_OK;
  if (!s_card) error = sd_mount_with_retry(true);
  if (error == ESP_OK && s_card) {
    esp_vfs_fat_mount_config_t format = {
      .format_if_mount_failed = false,
      .max_files = 8,
      .allocation_unit_size = 32 * 1024,
      .disk_status_check_enable = true,
      .use_one_fat = false,
    };
    ESP_LOGW(TAG, "LY|STORAGE_FORMAT|event=begin filesystem=fat32 cluster=32768 destructive=1");
    error = esp_vfs_fat_sdcard_format_cfg(MOUNT_POINT, s_card, &format);
  }
  app_error_t layout = error == ESP_OK ? prepare_card_layout()
                                       : APP_ERR_STORAGE_WRITE;
  app_error_t selftest = layout == APP_ERR_NONE ? sd_speed_probe() : layout;
  if (error == ESP_OK && layout == APP_ERR_NONE && selftest == APP_ERR_NONE &&
      card_schema_current()) {
    s_recovery_blocked = false;
    s_storage_ready = true;
    ESP_LOGI(TAG, "LY|STORAGE_FORMAT|event=complete schema=%s result=ESP_OK",
             CARD_SCHEMA);
    return ESP_OK;
  }
  s_format_in_progress = false;
  ESP_LOGE(TAG,
           "LY|STORAGE_FORMAT|event=failed esp=%s layout=%d selftest=%d",
           esp_err_to_name(error), (int)layout, (int)selftest);
  return error != ESP_OK ? error : ESP_FAIL;
}
