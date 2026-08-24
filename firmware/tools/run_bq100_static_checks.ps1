[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$power = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\power_mgr\power_mgr.c') -Raw
$powerSoc = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\power_mgr\power_soc.c') -Raw
$pins = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\board_pins.h') -Raw
$audio = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\audio_pdm\audio_pdm.c') -Raw
$network = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$ui = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw

foreach ($required in @('BQ25186_BOOT_CHARGE_CURRENT_MA 1000',
                         'BQ25186_LOW_SOC_CHARGE_MA    1000',
                         'BQ25186_MID_SOC_CHARGE_MA    1000',
                         'BQ25186_HIGH_SOC_CHARGE_MA   1000',
                         'BQ25186_LOW_SOC_INPUT_MA     1050',
                         'BQ25186_MID_SOC_INPUT_MA     1050',
                         'BQ25186_HIGH_SOC_INPUT_MA    1050',
                         'BQ_REG_ICHG_CTRL',
                         'BQ_REG_SYS_REG',
                         'bq25186_configure_with_retry',
                         '(ic_ctrl & ~0x03U) | 0x03U',
                         'bq_apply_charge_policy',
                         'sys_reg &= 0x1FU',
                         'sys=tracking',
                         '"1a_cc"',
                         'chg == 3) return APP_CHG_FULL',
                         'stable_chg == APP_CHG_FULL ? 100',
                         'stable_chg != APP_CHG_FULL && target_soc >= 100',
                         'power_diag_snapshot',
                         'LY|POWER_DIAG|source=power',
                         'GPIO_MODE_OUTPUT_OD',
                         'gpio_set_level(PIN_BQ_CE_N, 0)',
                         'candidate_count >= 2',
                         'have_charge_sample',
                         'sys_track=%d dppm_en=%d ilim_active=%d vdppm=%d vindpm=%d therm=%d')) {
    if (($pins + $power) -notmatch [regex]::Escape($required)) {
        throw "BQ25186 adaptive-charge patch is missing: $required"
    }
}
$storage = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\storage_sd\storage_sd.c') -Raw
foreach ($required in @('POWER_DIAG_PATH DIAG_ROOT "/power.csv"',
                         'power_diag_task',
                         'epoch_utc,uptime_s,sequence,recording,gauge_soc',
                         'LY|SD_EXPORT_DATA|seq=',
                         'power_export',
                         'mbedtls_sha256_finish',
                         'recording_active')) {
    if ($storage -notmatch [regex]::Escape($required)) {
        throw "Offline power diagnostics are missing: $required"
    }
}
foreach ($required in @('{50, 28}', '{60, 42}', '{70, 58}', '{80, 74}',
                         'power_soc_calibrate_x256')) {
    if ($powerSoc -notmatch [regex]::Escape($required)) {
        throw "Measured SOC calibration is missing: $required"
    }
}
foreach ($required in @('reported_step_tick', 'target_soc <= 10', 'mv <= 3550',
                         'pdMS_TO_TICKS(60 * 1000)')) {
    if ($power -notmatch [regex]::Escape($required)) {
        throw "Safe SOC display slew limiting is missing: $required"
    }
}
foreach ($required in @('LAN_WIFI_SSID       "TP-LINK_184F"',
                         'LAN_SERVER_URL      "http://192.168.31.183"',
                         'PUBLIC_SERVER_URL   LUOYE_CFG_SERVER_BASE_URL',
                         'server_base_url()',
                         'route=%s server=%s')) {
    if ($network -notmatch [regex]::Escape($required)) {
        throw "WiFi route policy is missing: $required"
    }
}
foreach ($required in @('capture_task', 'audio_cap', 'AUDIO_PDM_PCM_GAIN')) {
    if ($audio -notmatch [regex]::Escape($required)) {
        throw "Original lightweight audio path changed: $required"
    }
}
foreach ($required in @('DEFAULT_UI_TIMEZONE_OFFSET_MINUTES 480',
                         'int32_t offset_minutes = DEFAULT_UI_TIMEZONE_OFFSET_MINUTES')) {
    if ($ui -notmatch [regex]::Escape($required)) {
        throw "UTC+8 standby-clock fallback is missing: $required"
    }
}
if ($audio -match 'esp_afe|esp-sr|AFE_TYPE|afe_handle|afe_feed|afe_fetch') {
    throw 'ESP-SR AFE must not be present in the v0.8.0 low-complexity build.'
}

Write-Output 'BQ25186 1A charge, Charge Done, offline diagnostics + WiFi route checks passed'
