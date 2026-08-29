[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$net = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\net_uploader.c') -Raw
$form = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\provisioning_form.c') -Raw
$ui = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw
$cmake = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'CMakeLists.txt') -Raw
$netConfig = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'components\net_uploader\luoye_net_config.h.in') -Raw
$buildConfig = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\luoye_build_config.h.in') -Raw
$buildInfo = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'main\luoye_build_info.c') -Raw

if ($net -notmatch "s_pair\.ap_password\[0\] = '\\0'" -or
    $net -notmatch 'ap\.ap\.authmode = WIFI_AUTH_OPEN' -or
    $net -match 'ap\.ap\.authmode = WIFI_AUTH_WPA2_PSK' -or
    $ui -notmatch '15_pair_hotspot.*password') {
    throw 'Provisioning SoftAP must be open and expose the no-password display field.'
}

if ($cmake -notmatch 'LUOYE_SERVER_BASE_URL\s+"http://clearmeeting\.chat:34567"' -or
    $cmake -notmatch 'LUOYE_ALLOW_INSECURE_HTTP' -or
    $cmake -notmatch 'Plain HTTP is restricted to dev/engineering builds' -or
    $netConfig -notmatch 'LUOYE_CFG_SERVER_BASE_URL' -or
    $net -notmatch 'minimum_firmware' -or
    $net -notmatch 'firmware_at_least' -or
    $net -notmatch 'provisioning_pair_restart_required' -or
    $net -notmatch 'auth_repair_binding' -or
    $net -notmatch 's_pair_requested = true' -or
    $net -notmatch 'bootstrap_clock_from_server' -or
    $net -notmatch 'bootstrap_clock_from_candidate' -or
    $net -notmatch 'client_time_utc' -or
    $net -notmatch 'Math\.floor\(Date\.now\(\)/1000\)' -or
    $net -notmatch 'cloud_transport_clock_ready' -or
    $net -notmatch 'device_auth_profile' -or
    $net -notmatch 'luoye_build_device_auth_profile' -or
    $net -notmatch 'ENGINEERING' -or
    $net -notmatch 'server_time_utc' -or
    $net -notmatch 'portal_stop\(\)' -or
    $net -notmatch 'esp_wifi_set_mode\(WIFI_MODE_STA\)' -or
    $form -notmatch 'provisioning_parse_utc_iso8601' -or
    $form -notmatch 'provisioning_parse_client_unix_utc' -or
    $form -notmatch 'provisioning_https_clock_ready' -or
    $form -notmatch 'provisioning_clock_bootstrap_required' -or
    $form -notmatch 'PAIRING_CODE_IN_USE' -or
    $form -notmatch 'PAIRING_EXPIRED' -or
    $net -notmatch 'binding_generation >= s_binding_generation' -or
    $net -notmatch 's_online && !s_idle_suspended && s_pair_requested') {
    throw 'Server origin must match the engineering public route and gate HTTP explicitly.'
}

if ($cmake -notmatch 'LUOYE_DEVICE_AUTH_PROFILE\s+"engineering"' -or
    $buildConfig -notmatch 'LUOYE_CFG_DEVICE_AUTH_PROFILE' -or
    $buildInfo -notmatch 'auth_profile=%s') {
    throw 'Engineering authentication profile must be frozen into the build identity.'
}

$portalProvision = [regex]::Match(
    $net,
    '(?s)static esp_err_t portal_provision\(.*?\n}\r?\n\r?\nstatic esp_err_t portal_redirect_404'
).Value
if (-not $portalProvision -or
    $portalProvision.IndexOf('bootstrap_clock_from_candidate') -lt 0 -or
    $portalProvision.IndexOf('start_sta_candidate') -lt 0 -or
    $portalProvision.IndexOf('bootstrap_clock_from_candidate') -gt
      $portalProvision.IndexOf('start_sta_candidate')) {
    throw 'SoftAP browser time must be applied before WiFi can start the first API request.'
}

$buildExchange = [regex]::Match(
    $net,
    '(?s)static esp_err_t build_info_exchange\(.*?\n}\r?\n\r?\nstatic bool pair_response_state'
).Value
if (-not $buildExchange -or
    $buildExchange -notmatch 'device_auth_profile' -or
    $buildExchange -notmatch 'luoye_build_device_auth_profile') {
    throw 'Build-info must fail closed unless device_auth_profile=engineering matches.'
}

$agendaSync = [regex]::Match(
    $net,
    '(?s)static uint32_t agenda_sync_once\(.*?\n}\r?\n\r?\nstatic uint32_t todo_upload_audio'
).Value
if (-not $agendaSync -or
    $agendaSync -notmatch 'response_binding == s_binding_generation' -or
    $agendaSync -notmatch 'error == ESP_OK \|\| error == ESP_ERR_INVALID_STATE') {
    throw 'Agenda server time must support cached revisions without crossing binding generations.'
}

foreach ($required in @('invalidate_binding_for_auth_repair',
                         'agenda=preserved',
                         'agenda_reset_binding(s_binding_generation)')) {
    if ($net -notmatch [regex]::Escape($required)) {
        throw "Auth repair must preserve the confirmed-generation agenda: $required"
    }
}
$authRepair = [regex]::Match(
    $net,
    '(?s)static void invalidate_binding_for_auth_repair\(.*?\n}\r?\n\r?\nstatic void auth_repair_binding'
).Value
if (-not $authRepair -or $authRepair -match 'agenda_reset_binding\s*\(' -or
    $authRepair -match 'nvs_erase_key\(nvs,\s*"binding_gen"\)') {
    throw 'A 401/403 token repair must not erase the agenda or binding generation.'
}

$todoRetry = [regex]::Match(
    $net,
    '(?s)static uint32_t todo_retry\(.*?\n}\r?\n\r?\nstatic uint32_t agenda_sync_once'
).Value
if (-not $todoRetry -or
    $todoRetry -match 'LUOYE_UPLOAD_HTTP_AUTH\).*?LUOYE_TODO_FAILED') {
    throw 'Authentication loss must preserve the durable voice-todo stage.'
}

if ($net -match 'clear_binding\(\);\s*\r?\n\s*pair_set_state\(NET_PAIR_ERROR') {
    throw 'Authentication loss must restart pairing instead of becoming a dead-end error.'
}

$cloudRequest = [regex]::Match(
    $net,
    '(?s)static esp_err_t cloud_request\(.*?\n}\r?\n\r?\nstatic uint32_t retry_item'
).Value
if (-not $cloudRequest -or $cloudRequest -match 'cloud_set_ready') {
    throw 'Ordinary object requests must not disable the global cloud compatibility gate.'
}

foreach ($field in @('account', 'username', 'user_id', 'server')) {
    if ($net -match ('<input[^>]+name=\\"' + [regex]::Escape($field) + '\\"')) {
        throw "SoftAP portal must not accept '$field'."
    }
}

$logLeak = $net -split "`r?`n" |
    Where-Object { $_ -match 'ESP_LOG[A-Z]*\s*\(' -and
                   $_ -match '(?i)password|device_token|nonce|masked_account' }
if ($logLeak) {
    throw "Sensitive provisioning value referenced by log statement: $($logLeak -join ' | ')"
}

foreach ($pattern in @('\u8bf7\u63d2\u5165SD\u5361',
                        '\u63d2\u5361\u540e',
                        '\u8bf7\u52ff\u62d4\u5361',
                        '\u8bf7\u52ff\u62d4\u51faSD\u5361',
                        '\u91cd\u65b0\u63d2\u5361',
                        '\u66f4\u6362SD\u5361',
                        '\u68c0\u67e5\u5361\u5ea7')) {
    if ($ui -match $pattern) {
        throw "Fixed-SD UI still contains removable-card instruction: $pattern"
    }
}

if ($cmake -notmatch 'PROJECT_VER\s+"1\.7\.0"' -or
    $cmake -notmatch 'luoye-device-api/2') {
    throw 'Firmware version or API contract does not match v1.7.0.'
}

Write-Output 'provisioning static checks passed'
