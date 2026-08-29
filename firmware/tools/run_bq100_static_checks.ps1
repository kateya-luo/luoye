[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$power = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\power_mgr\power_mgr.c') -Raw
$powerSoc = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\power_mgr\power_soc.c') -Raw
$state = (Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.c') -Raw) +
         (Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\app_state.h') -Raw)
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
                         'source=charger_only',
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
foreach ($required in @('{3180, 0}', '{3500, 10}', '{3800, 57}',
                         '{4000, 90}', '{4100, 100}',
                         'power_soc_from_voltage',
                         'POWER_SOC_VOLTAGE_WINDOW',
                         'power_soc_display_update',
                         'median_voltage',
                         'return power_soc_from_voltage(filtered_mv)')) {
    if ($powerSoc -notmatch [regex]::Escape($required)) {
        throw "Passive voltage display mapping is missing: $required"
    }
}
foreach ($required in @('BATTERY_EMERGENCY_MV 3100',
                         'BATTERY_EMERGENCY_SAMPLES 3',
                         'APP_EV_BATTERY_CRITICAL',
                         'action=safe_close_if_recording')) {
    if (($power + $state) -notmatch [regex]::Escape($required)) {
        throw "Physical low-voltage data-integrity guard is missing: $required"
    }
}
foreach ($removed in @('BATTERY_RECORD_MIN_PERCENT',
                        'BATTERY_CRITICAL_PERCENT', 'battery_low_latched')) {
    if ($state -match [regex]::Escape($removed)) {
        throw "Percentage-based feature restriction is still present: $removed"
    }
}
foreach ($removed in @('power_soc_calibrate_x256',
                        'power_soc_discharge_tail_floor',
                        'bq_target_for_soc', 'control_soc',
                        'POWER_SOC_CONFIRM_SAMPLES',
                        'stable_target_soc', 'candidate_soc',
                        'reported_soc', 'last_step_ms',
                        'charge_done', 'target_soc > 99',
                        '30000U', '60000U')) {
    if (($power + $powerSoc) -match [regex]::Escape($removed)) {
        throw "Legacy SOC-coupled behavior is still present: $removed"
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

Write-Output 'BQ25186 1A charge + direct voltage display + offline diagnostics checks passed'
