# 落叶（Luoye）录音卡固件

ESP32-S3 + 1.54" 方形墨水屏(GDEY0154D67/SSD1681, 200×200)+ 双 PDM 麦 + microSD。

当前方屏版本说明、按键表和烧录步骤见 [docs/UI154_FIRMWARE.md](docs/UI154_FIRMWARE.md)。
内部产品名为 **落叶 / Luoye**，ESP-IDF 工程名保留为 `recorder_card`。
当前固件版本：`2.3.2`，设备协议：`luoye-device-api/2`，配套服务器：ClearMeeting `1.0.1`。
交互行为以 **250×122 交互模拟器** 为规格书(`ai-recorder-card-sim-index-250x122.html`),
`main/app_state.c` 是其中状态机的 1:1 C 移植 —— **改交互先改模拟器,确认后两边同步**。

## 架构

```
keys(10ms轮询) ──┐
power_poll(5s) ──┼─→ [app事件队列] → app_task(串行) → app_state 状态机 → hooks
network scheduler ─┘                                        │
                                                   ┌────────┼─────────┐
audio_cap(核1,p18) → StreamBuffer(PSRAM 256KB) → sd_writer(核0,p15)   │
                                                   ui(渲染,p6)   led(50ms)
```

数据流：
**PDM 双麦 → 16k 单声道 PCM → SD(WAV,先落卡) → 在线 160 KiB 实时上传；
整段离线/断网补洞按 10 MiB 逻辑范围、64 KiB RAM 流式上传
→ 最多 4 秒一次 revision 游标轮询字幕/译文 → 墨水屏节流回屏**。议程以 UTC/revision 缓存到固定 SD，
PCF8563 调度最近提醒；待机按住待办键可另存最长 30 秒的语音待办。当前录音在线时
实时上传，已结束的历史录音由录音键长按进入、待办键确认的 FIFO 同步；服务器验证字节覆盖并
完整确认后立即删除对应本地目录。录音与待办请求都使用稳定幂等键。所有已认证
HTTP 由单一网络业务调度器仲裁，SD 录音写入不依赖网络。

## 目录

| 路径 | 内容 |
|---|---|
| `main/board_pins.h` | **引脚真源**(网表+BOM 验证,2026-07-10),改板先改这里 |
| `main/luoye_build_info.[ch]` | 单一版本源、commit、硬件版本、IDF 和启动身份日志 |
| `main/luoye_diag.[ch]` | 统一 `LYE-xxx` 错误码、子系统状态和事件丢弃计数 |
| `main/app_state.[ch]` | 核心状态机(纯逻辑,可 PC 单测) |
| `main/input_keys.c` | 三键去抖 + 每键每态长按阈值（录音键1.5s/关机3s，设置键3s，待办键待机600ms） |
| `main/led_ctrl.c` | 三灯花样(语义同模拟器 renderLeds) |
| `main/ui_render.c` | 帧缓冲 + 各屏布局(中文文案对齐模拟器);5×7 ASCII 兜底 |
| `main/ui_font.c` | 16px 中文点阵字库加载与渲染(assets 分区 `font16.bin` → PSRAM,二分查找) |
| `tools/gen_font.py` | 字库生成脚本(TTF/TTC → font16.bin) |
| `tools/preview_screens.py` | 屏幕布局预览(不烧板看 250×122 真机像素效果) |
| `tools/test_app_state.c` | 状态机 PC 回归测试(`run_state_test.bat` 一键编译运行,改 app_state.c 必跑) |
| `tools/build_profile.ps1` | 固化 dev / rc / release 三种构建配置 |
| `tools/run_engineering_checks.ps1` | 回归测试、版本检查、构建和可选打包门禁 |
| `tools/package_firmware.ps1` | 生成带 manifest、SHA-256 和烧录说明的内部发布包 |
| `assets/font16.bin` | 21159 字 / 703KB(Noto Sans SC Light，GBK 全集汉字 + 中英标点 + 全角) |
| `components/epd_ssd1680/` | 屏驱动(厂商时序逐条移植;全刷 0xF7 / 快刷 0xC7 / 局刷 0xFF) |
| `components/audio_pdm/` | I2S PDM 采集 → PSRAM 环形缓冲 |
| `components/storage_sd/` | SD 挂载 + WAV 写入(64KB 修头+fsync,断电可恢复)+ 容量巡检 |
| `components/net_uploader/` | SoftAP、账号认领、持久补传、实时字幕/翻译协议 |
| `components/power_mgr/` | BQ25186 / MAX17048 / PCF8563(I2C)+ 深睡 |
| `components/agenda_todo/` | 议程原子缓存、RTC 调度、语音待办旁路录制与状态持久化 |

## 构建（固定 ESP-IDF v5.5.4）

在 ESP-IDF PowerShell 中执行：

```powershell
cd D:\OPENOP\recorder-card-hw-test\firmware\recorder-card-v070-100ma
.\tools\run_engineering_checks.ps1 -Profile dev -FullClean
```

正式 HTTPS 开发构建（默认连接 `https://clearmeeting.chat`）：

```powershell
.\tools\build_profile.ps1 -Profile dev
```

局域网/公网工程联调必须显式给出服务器 origin 并打开明文 HTTP；该开关只允许 `dev` 或 `engineering`
工程构建，不能用于真实账号或敏感录音：

```powershell
.\tools\build_profile.ps1 -Profile dev `
  -ServerBaseUrl http://192.168.1.100:34567 `
  -AllowInsecureHttp
```

`ServerBaseUrl` 只能是无路径、无末尾 `/` 的 `http(s)://host[:port]`。
`rc` / `release` 构建拒绝 HTTP，并固定使用系统 CA 验证 HTTPS。

生成工程烧录包（必须先提交源码并保持 Git 工作树干净）：

```powershell
.\tools\run_engineering_checks.ps1 -Profile engineering -FullClean -Package `
  -ReleaseId luoye-fw-v2.3.2-engineering-live-io-r1 `
  -ServerBaseUrl http://clearmeeting.chat:34567 -AllowInsecureHttp
```

当前正式构建机已使用 ESP-IDF v5.5.4 完成 baseline 干净构建。构建会自动把
`assets/`（含字库）生成 `assets.bin`；打包脚本不会擦除或覆盖设备 NVS。

版本单一真源是根 `CMakeLists.txt` 的 `PROJECT_VER`。UI、ESP app descriptor、
串口 `LY|BOOT` 记录和 release manifest 必须保持一致。

当前硬件标识暂定为 `LY-HW-ENG-20260710`，表示基于 2026-07-10 网表的工程样板；
正式 PCB 丝印版本确定后必须同步更新，禁止把暂定标识用于量产。

## 中文字库

- 运行时:`ui_font.c` 开机把 `font16.bin` 载入 PSRAM;文件缺失时 UI 自动退回 ASCII(中文显示空心框),设备不砖。
- 重新生成:`python tools/gen_font.py --font <字体文件> --out assets/font16.bin`,
  `--test 会议` 可先预览点阵,`--dump 会议` 验证已生成文件的解码。
- **字体授权**:当前 `font16.bin` 使用 Noto Sans SC Light 生成；固件页面使用单色点阵，
  不包含灰阶抗锯齿。

## 首次点亮前必读

1. **墨水屏方向校准**:若首帧镜像/颠倒,改 `epd_ssd1680.h` 里
   `EPD_ROT_FLIP_X / EPD_ROT_FLIP_Y / EPD_X_OFFSET` 三个宏即可,不用动代码。
2. **Strap 脚**(引脚表里已标 ⚠):
   - GPIO46(EPD_MOSI)启动瞬间必须为低 —— 屏端不得有上拉;
   - GPIO45(FULL_LED)、GPIO3(EPD_SCLK)同为 strap,当前用法安全,改板别挂强上拉。
3. **到点提醒的低功耗策略**:`RTC_INT` 在 **GPIO41(非 RTC-IO)**，不能作为深睡
   唤醒源。固件在存在已缓存提醒时改用 GPIO light sleep，由 GPIO41 或长按 REC
   唤醒；没有提醒时仍进入深睡。这样现板无需飞线即可到点唤醒，代价是有提醒期间
   待机功耗高于深睡。下一版 PCB 仍建议把 `RTC_INT` 改到 RTC-IO。
4. 按键无外部上拉(只有 100nF 去抖电容):运行态开内部上拉,深睡前开 RTC 域上拉
   (`power_enter_off` 已处理,别删)。

## 与模拟器的行为对照

| 模拟器 | 固件 |
|---|---|
| 待机短按 REC 开录 / BACK 翻页 | `app_state.c: short_press` |
| 录音短按 REC 暂停 / 长按 REC 1.5s 结束 | 同上 + 安全收尾 |
| 待机按住 REC 说话建待办，松开保存 | 独立截取单声道 WAV；断网上传、ASR 与确认状态均持久化 |
| 中间键 | 待机长按进入同步页；短按确认上传历史录音；BACK 取消 |
| 录音长按 BACK 3s 锁键;待机长按 BACK 配网 | `long_press` |
| 录音开始立即显示；章节纪要按自然分钟边界刷新；待机分钟刷新 | 所有产品页面统一使用 SSD1681 差分快刷 |
| RTC 到点提醒(REC 开录 / BACK 关闭 / 长按 BACK +10min) | `rtc_alarm` + PCF8563 闹钟 |
| 电量 <5% 拒绝开录;SD 将满 FULL 黄灯 | `start_rec` / `space_task` |

## 已知限制（工程版，量产前必修）

- 固件 v1.0.0 和 ClearMeeting v0.19.2 已统一到 `luoye-device-api/2`；账号隔离、配对、断网补传、
  字幕/翻译、议程和语音待办仍需完成真实设备端到端验收后才能进入量产状态。
- 设备端只读取服务器给出的同一 revision 原文/译文；不在本地运行 ASR 或翻译模型。
- PCF8563 闹钟没有月份字段；服务器议程接口应只下发未来 14 天窗口。
- 状态快照跨任务读（UI/LED 读 app_state）对 64 位字段仍有理论撕裂风险。
- 有提醒时使用 light sleep 是当前硬件兼容方案，需在真板测量实际待机电流。

## TODO(按优先级)

1. ~~中文渲染~~ ✅ 已完成(16px 点阵字库 + UTF-8 渲染 + 折行;量产前换开源字体重新生成)。
2. ~~SoftAP 配网和账号认领客户端~~ ✅ API/2 客户端完成；待真板端到端验收。
3. ~~实时字幕/翻译协议与会议两页、翻译三页~~ ✅ API/2 客户端完成；待真板端到端验收。
4. ~~语音待办：MARK 按住期间单独截 PCM → 断网补传 → 确认后入日历~~ ✅ API/2 客户端完成；待真板端到端验收。
5. ~~SNTP 校准 PCF8563、议程原子缓存和最近提醒调度~~ ✅ 设备端完成；待真板端到端验收。
6. OTA(切双 app 分区);音频编码抽象(Opus/ADPCM 省流量)。
