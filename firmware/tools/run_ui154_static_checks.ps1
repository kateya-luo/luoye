$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$assetDir = Join-Path $project 'assets\ui154'
$ui = Get-Content -LiteralPath (Join-Path $project 'main\ui_render.c') -Raw -Encoding utf8
$state = Get-Content -LiteralPath (Join-Path $project 'main\app_state.c') -Raw -Encoding utf8
$keys = Get-Content -LiteralPath (Join-Path $project 'main\input_keys.c') -Raw -Encoding utf8
$driver = Get-Content -LiteralPath (Join-Path $project 'components\epd_ssd1681\epd_ssd1681.c') -Raw -Encoding utf8
$driverHeader = Get-Content -LiteralPath (Join-Path $project 'components\epd_ssd1681\include\epd_ssd1681.h') -Raw -Encoding utf8
$topCmake = Get-Content -LiteralPath (Join-Path $project 'CMakeLists.txt') -Raw -Encoding utf8
$layoutSource = Get-Content -LiteralPath (Join-Path $project 'main\ui154_layout_generated.c') -Raw -Encoding utf8
$fontSource = Get-Content -LiteralPath (Join-Path $project 'main\ui_font.c') -Raw -Encoding utf8
$manifest = Get-Content -LiteralPath (Join-Path $assetDir 'manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json

$bins = @(Get-ChildItem -LiteralPath $assetDir -Filter '*.bin' -File)
if ($bins.Count -ne 23) { throw "Expected 23 UI assets, got $($bins.Count)" }
foreach ($bin in $bins) {
    if ($bin.Length -ne 5000) { throw "Invalid 1-bit UI asset size: $($bin.Name)=$($bin.Length)" }
    if (('/ui154/' + $bin.Name).Length -ge 32) {
        throw "SPIFFS object name too long: $($bin.Name)"
    }
}

foreach ($required in @(
    'epd_ssd1681.h', 'EPD_FB_BYTES',
    'epd_frame_fast', 'BW-FAST',
    'ui154_layout_generated.h', 'load_layout_page', 'layout_draw_field_latest',
    '05_meeting_caption', '15_pair_hotspot', '22_storage_error',
    'LY|UI_RECORD|refresh=',
    'seconds / 5', 'five_second % 60', 'periodic_fast',
    'render_once_panel', 'epd_frame_fast',
    'epd_frame_partial_window(s_fb, 4, 8, 192, 171)')) {
    if ($ui -notmatch [regex]::Escape($required)) { throw "UI missing: $required" }
}
if ($manifest.pages.Count -ne 23) { throw "Manifest page count mismatch: $($manifest.pages.Count)" }
if ($manifest.format -ne 'luoye-ui154-1bpp-v1' -or $manifest.black_bit -ne 1) {
    throw 'UI assets are not the approved packed 1-bit black/white format'
}
if ($manifest.layout_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Manifest layout SHA256 missing' }
if ($layoutSource -notmatch [regex]::Escape($manifest.layout_sha256)) {
    throw 'Generated text layout and binary assets do not come from the same JSON'
}
if ($layoutSource -match '\{"11_todo_confirm", "time"' -or
    $layoutSource -match '\{"12_todo_created", "time"') {
    throw 'Todo pages still expose due-time fields'
}
if ($ui -match 'epd_ssd1680') { throw 'UI still includes SSD1680' }
foreach ($required in @('update(0xC7', 'update(0xB1', 'data(0x64)', 'update(0x91',
                         'update(0xF7', 'update(0xFF', 'set_partial_window',
                         'write_partial_ram', 's_base_ready', 'init_display_fast',
                         'data_buffer(s_panel, EPD_FB_BYTES)',
                         's_last_error = ESP_ERR_TIMEOUT')) {
    if ($driver -notmatch [regex]::Escape($required)) { throw "Driver missing: $required" }
}
if ($driverHeader -notmatch '#define\s+EPD_SWAP_XY\s+1' -or
    $driverHeader -notmatch '#define\s+EPD_FLIP_X\s+1' -or
    $driverHeader -notmatch '#define\s+EPD_FLIP_Y\s+1') {
    throw 'Requested rotate/mirror transform is not enabled'
}
if (($driver + $driverHeader) -match '(?i)gray4|EPD_GRAY|2-bpp|panel_aux|partial_old') {
    throw 'SSD1681 driver still contains a four-gray path or extra gray buffer'
}
if ($topCmake -notmatch 'set\(EXCLUDE_COMPONENTS\s+epd_ssd1680\)') {
    throw 'Legacy SSD1680 component can still override SSD1681 at link time'
}
foreach ($size in @(8,9,10,11,12,13,14,16,18,20,22,24,26,32,37,40,51)) {
    $font = if ($size -eq 16) { 'font16.bin' } else { ('font_{0:D2}.bin' -f $size) }
    $path = Join-Path $project "assets\$font"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing native font strike: $font" }
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -lt 16 -or [Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne 'CMF1') {
        throw "Invalid native font strike: $font"
    }
    $cell = [BitConverter]::ToUInt16($bytes, 4)
    if ($cell -ne $size) { throw "Native font size mismatch: $font declares $cell" }
    $count = [BitConverter]::ToUInt32($bytes, 8)
    if ($size -eq 24 -and $count -lt 20000) {
        throw "24px dynamic chapter-title font is not full CJK: only $count glyphs"
    }
}
foreach ($required in @('family=SimSun', 'mode=mono-native', 'antialias=off', 'scaling=off', 'draw_native_glyph')) {
    if ($fontSource -notmatch [regex]::Escape($required)) { throw "Native font renderer missing: $required" }
}
if ($fontSource -match 'draw_scaled_glyph') { throw 'Arbitrary glyph scaling is still present' }
foreach ($required in @('PIN_KEY_MARK', 'APP_EV_KEY_MARK_RELEASE', 'has_release = true')) {
    if ($keys -notmatch [regex]::Escape($required)) { throw "Todo-key release flow missing: $required" }
}
foreach ($required in @('APP_EV_KEY_MARK_RELEASE', 'todo_hold()', 'todo_release()',
                         'Long MARK used to switch to the rolling-minutes page')) {
    if ($state -notmatch [regex]::Escape($required)) { throw "MARK todo flow missing: $required" }
}
if (($ui + $state) -match 'APP_RECORD_VIEW_TIMELINE|record_view') {
    throw 'Transcript-only recording UI still exposes the removed rolling-minutes view.'
}

[pscustomobject]@{
    Panel = 'GDEY0154D67 / SSD1681'
    Resolution = '200x200'
    StaticAssets = 'black/white / 1-bpp'
    Assets = $bins.Count
    AssetBytes = ($bins | Measure-Object Length -Sum).Sum
    NativeFontStrikes = 17
    FontFamily = 'SimSun'
    FontRender = 'direct 1-bit'
    RuntimeScaling = 'off'
    RecordKey = 'tap=record/pause; hold=sync standby/end recording'
    TodoKey = 'tap=agenda/mark/confirm; hold-and-release=voice todo'
    SettingsKey = 'tap=status/back; hold=pair/lock/snooze'
    RecordingRefresh = 'window PARTIAL every 5s; whole-panel FAST every 5min'
    LayoutSha256 = $manifest.layout_sha256
    Result = 'PASS'
}
