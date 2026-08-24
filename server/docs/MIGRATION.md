# ClearMeeting 整体迁移手册（ClearMeeting v0.12.0 / 落叶 API v1）

> 这份文档回答一个问题：**把整套 ClearMeeting 搬到一台新电脑/新服务器，需要做什么？**
> 服务器部署细节在 [DEPLOYMENT.md](DEPLOYMENT.md)；本文负责全局：有哪些组成部分、
> 每部分怎么构建/部署、密钥与数据怎么搬、以及按端分组的**全部踩坑索引**。

---

## 1. v0.12.0 里程碑快照（这套系统现在能做什么）

- **实时字幕**：FunASR 流式识别（中文/中英混合/纯英文），毫秒级现场字幕
- **实时翻译（M2）**：逐句 LLM 翻译（串行有序 + 上下文窗口 + 同语言跳过），
  场景：①中英混合会议 ②学生听英文课（EN→ZH）；VAD 12s 保证连续讲话及时断句
- **说话人分离**：CAM++ 声纹实时分离 + 会后二次校正
- **断网零丢字（双通道）**：通道A 实时流（允许丢）+ 通道B 可靠分片上传；
  断网段由离线转写自动补洞原位插回
- **AI 纪要**：DeepSeek 滚动纪要/摘要/待办/决策，会中实时更新
- **多端**：浏览器（PC+手机，PWA）、Windows 桌面（托盘常驻+录音悬浮窗+双语 Word 导出）、
  Android WebView 壳
- **设备接入（M1）**：配对→令牌→会话→分片上传的完整 HTTP/WS 契约 + 假录音笔联调脚本
- **硬件两条线**：BLE 录音笔（nRF52840，协议 V1 + Windows BLE agent）；
  WiFi 录音卡（ESP32-S3 + 墨水屏，引脚已验证、固件骨架 + 中文字库 + 状态机回归测试完成，待真板）

---

## 2. 目录结构（每个目录是什么、入口在哪）

```
clearmeeting/
├── README.md                  ← 项目总览（先读这个）
├── package.json               ← npm workspace 根（桌面端打包用 npm 走这里）
├── pnpm-workspace.yaml        ← pnpm workspace（网页端开发用 corepack pnpm）
├── pnpm-lock.yaml / package-lock.json   ← 两把锁并存：pnpm=网页开发, npm=桌面打包（见 §3.3）
├── apps/
│   ├── web-client/            ← React+Vite 前端（浏览器/手机页面，也是桌面客户端的 UI）
│   ├── desktop-client/        ← Electron 桌面端 v0.20（main.cjs 入口；native/ 是 C# BLE agent）
│   ├── android-client/        ← Android WebView 壳工程
│   ├── webpen/                ← 浏览器版"录音笔"模拟页（BLE 联调）
│   └── card-sim/              ← 录音卡交互模拟器（250×122 真机像素，固件的行为规格书）
├── server/                    ← FastAPI 服务端 + 单元测试 + fake_pen.py（假录音笔）
│   └── data/                  ← 运行时数据目录（部署后生成，迁移时要搬，见 §4.2）
├── deploy/                    ← docker-compose 全家桶 + nginx + .env.example + 安装脚本
├── firmware/
│   ├── nrf52840/              ← BLE 录音笔固件（Zephyr）
│   └── recorder-card/         ← ESP32-S3 录音卡固件（ESP-IDF；tools/ 含字库生成+屏幕预览+状态机测试）
├── protocol/                  ← BLE 协议金标测试向量（C/C# 双实现一致性验证）
└── docs/                      ← ARCHITECTURE / DEPLOYMENT / MIGRATION / PLATFORM_ROADMAP / RECORDER_CARD_SPEC
```

**没有冗余的保证**：demo/开发页（`demo.html`、`captionTest` 等）只在源码库存在，不进本发布包；
构建产物（`node_modules/`、`dist/`、`app/client/`、`bin/obj/`）一律不进包，由构建命令现场生成。

---

## 3. 各端构建与部署

### 3.1 服务器（必做，其他端都依赖它）

按 [DEPLOYMENT.md](DEPLOYMENT.md) 从第 0 节走到第 5 节即可跑通；
外网访问看第 7 节，GPU 离线加速看第 8 节。

### 3.2 网页端 / 移动端（零部署）

网页端**随服务器一起部署**（nginx 容器里带着构建好的前端），手机浏览器直接访问同一地址，
不存在独立的"移动端部署"。要本地开发调试才需要：

```bash
corepack enable                        # 用仓库锁定的 pnpm 版本
corepack pnpm install
corepack pnpm --filter @ai-recorder/web-client dev    # Vite 开发服务器（代理 /ws /api 到 localhost:8000）
```

⚠️ **坑**：本仓库是 pnpm workspace，**别用 `npm install` 装网页端依赖**（会生成冲突的
node_modules 布局）；改了前端要上服务器，走 DEPLOYMENT §9 三步铁律 `build nginx`。

### 3.3 Windows 桌面端（v0.20 打包）

```powershell
# 普通 PowerShell 即可；如报符号链接权限错误 → 开"开发者模式"或用管理员窗口
$env:Path = "C:\Program Files\nodejs;" + $env:Path
$env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://registry.npmmirror.com/-/binary/electron-builder-binaries/"
cd apps\desktop-client
npm.cmd install          # 首次
npm.cmd run pack:win     # = 构建网页UI → dotnet 发布 BLE agent → electron-builder 打便携 exe
# 产物：apps/desktop-client/dist/Clear Meeting 0.20.0.exe
```

依赖：Node.js ≥18、.NET SDK（BLE agent 用 `dotnet publish` win-x64）。

**踩过的坑**：
1. `winCodeSign` 解压符号链接失败 → Windows 需要**开发者模式**（设置→系统→开发者选项）
   或管理员 PowerShell。
2. electron-builder 从 GitHub 下载二进制超时 → 设上面的
   `ELECTRON_BUILDER_BINARIES_MIRROR` 走 npmmirror。
3. 管理员窗口里 `npm` 不在 PATH → 先把 `C:\Program Files\nodejs` 加进 `$env:Path`，用 `npm.cmd`。
4. **改了前端代码但导出的 Word/界面还是旧的** → exe 里打包的是构建时的 `app/client`，
   必须重新 `pack:win`（它会先重建 UI）再换新 exe。验证法：导出的 .doc 里能搜到新标记即新包。
5. 桌面端连 http 服务器时靠 `unsafely-treat-insecure-origin-as-secure` 开关拿麦克风权限
   （main.cjs 已处理，换服务器地址后要在设置页重存一次）。

### 3.4 Android

Android Studio 打开 `apps/android-client`，改 `MainActivity.java` 里的默认服务器地址
（或首次启动时输入），连真机 Run。就是个 WebView 壳，核心逻辑全在网页端。

### 3.5 BLE 录音笔（nRF52840）

- 固件：`firmware/nrf52840`（Zephyr 工程，见其 README）
- Windows 侧：BLE agent 随桌面端一起发布（§3.3 的 `pack:win` 已包含）
- **改协议必读**：`protocol/README.md`——C 固件与 C# agent 必须同一提交内同步改，
  金标验证：
  ```powershell
  python protocol/generate_test_vectors.py
  dotnet run --project apps/desktop-client/native/ClearMeeting.BleProtocol.Verifier -- protocol/test_vectors.json
  ```

### 3.6 WiFi 录音卡（ESP32-S3，固件骨架阶段）

```bash
cd firmware/recorder-card
idf.py set-target esp32s3 && idf.py build && idf.py -p COMx flash monitor
# assets/（含 702KB 中文字库）会随 flash 自动烧进 spiffs 分区
```

先读 `firmware/recorder-card/README.md`，特别是：
- **引脚真源** `main/board_pins.h`（按 2026-07-10 网表+BOM 三锚点验证）
- **三个 strap 脚**（GPIO46 屏 MOSI 启动须低 / GPIO45 / GPIO3）
- **RTC_INT 在 GPIO41 非 RTC-IO** → 关机态闹钟叫不醒，下版改板建议与 EPD_RST(GPIO21) 互换
- **字库授权**：当前 font16.bin 由 Windows 宋体生成，仅限原型；量产前用开源字体重跑
  `tools/gen_font.py`
- 改状态机必跑 `tools/run_state_test.bat`（PC 端回归,30+ 断言）;
  改屏幕布局用 `tools/preview_screens.py` 先看像素效果,不用烧板

### 3.7 模拟器与联调工具速查

| 工具 | 用途 | 打开方式 |
|---|---|---|
| `apps/card-sim/index.html` | 录音卡交互规格书（改固件交互前先改它） | 浏览器直接开 |
| `apps/webpen/index.html` | 浏览器模拟 BLE 录音笔 | 同上（或 bat） |
| `deploy/verify_api_v1.sh` | 无硬件验证服务版本与落叶 API v1 能力 | DEPLOYMENT §5 |
| `firmware/recorder-card/tools/*` | 字库生成/屏幕预览/状态机测试 | §3.6 |

---

## 4. 密钥与数据（迁移时最容易出事的部分）

### 4.1 密钥清单（**绝不进仓库/发布包**，逐台机器重新配置）

| 密钥 | 放哪 | 说明 |
|---|---|---|
| `TEST_ACCOUNT_PASSWORD` | 服务器 `deploy/.env` | 测试账号首次创建密码；必须在首启前设定强值，之后不自动覆盖 |
| `AUTH_SECRET` | 服务器 `deploy/.env` 或 SQLite `meta` | 账号 token 签名密钥 |
| `DEVICE_API_SECRET` | 服务器 `deploy/.env` 或 SQLite `meta` | 落叶设备 API 密钥；不得与 `AUTH_SECRET` 共用 |
| `DEEPSEEK_API_KEY` | 服务器 `deploy/.env` | 纪要+翻译 |
| DNSPod `login_token` | 服务器上的 DDNS 脚本（不在本仓库） | 迁移新服务器要重新部署 DDNS 脚本+cron |

`.gitignore` 已把 `deploy/.env`、`*.db`、`*.pcm`、`server/data/` 全部排除;
迁移打包前用这条自查（应无输出）：
```bash
grep -rniE "sk-[a-z0-9]{16,}|login_token" --include="*" . | grep -v node_modules | grep -v ".env.example"
```

### 4.2 数据搬家（换服务器时）

全部会议数据就两样，都在旧服务器 `server/data/`（compose 挂载卷）：

```bash
# 旧服务器：停服务再拷，保证 SQLite 一致性
cd ~/clearmeeting/deploy && docker compose down
tar -czf ~/cm-data.tar.gz -C ~/clearmeeting/server data
# 传到新服务器解包到相同位置，再启动
tar -xzf cm-data.tar.gz -C ~/clearmeeting/server
```

FunASR 模型缓存（`FUNASR_MODELS_DIR`，数 GB）**可搬可不搬**：搬省下载时间；不搬首启自动重下。
搬了模型缓存记得重做 DEPLOYMENT §8.4 的 torchscript 软链接检查。

---

## 5. 全部踩坑索引（按端分组，详情见括号内文档章节）

**服务器稳定性**
- 一代 Ryzen 空闲 C-state 随机硬冻结 → BIOS 禁用（DEPLOYMENT §1.5）
- sp5100_tco 看门狗被 Ubuntu 拉黑,常规加载静默失效 → systemd 强制 modprobe（§1.4）
- 无 swap 内存耗尽随机杀进程 → 8G swap + 关桌面（§1.1/1.2）

**Docker/FunASR**
- `ASR_MODE=mock` 假字幕（§4）
- funasr 每次启动 ~60s 加载模型,刚开机连不上（§5）
- VAD 12s 断句:全新安装首次下载模型后需 restart funasr 一次（§5）
- BladeDISC 编译卡死 20+ 分钟像死机（§8.4）
- torchscript 模型名不匹配,软链接要补**两个**,少一个 3GB 卡 OOM（§8.4）
- `tail -f` 保活假活,服务死容器 Up,自愈永不触发 → 监视进程（§8.4）
- pgrep 必须 `'[f]unasr-wss-server'` 防自匹配（§8.4）
- 离线协议:`audio_fs:16000` 必带/整段一次性返回/无逐句时间戳（§8.5）
- 构建日志 COPY 层 CACHED = 代码没同步到位（§9）
- 国内网络 docker build 走腾讯内网镜像域名解析失败 → `DOCKER_BUILDKIT=0` 应急,
  根治换 daemon.json registry-mirrors 为公共镜像

**公网访问**
- 电信封 80/443 入站 → 高端口转发,双层路由两级转发（§7）
- DNSPod `Record.List` 域名 id 排在记录 id 前,`head -1` 拿错 id（§7）
- NAT 回环:内网测公网域名不通,必须手机流量测（§7）
- HTTP 明文风险,设备令牌/音频过公网要 HTTPS（§6.1/§7）

**网页/移动端**
- pnpm workspace 别用 npm install 装依赖（本文 §3.2）
- http 下手机浏览器禁麦克风,夸克例外;其他浏览器要 HTTPS（DEPLOYMENT §10）
- 手机熄屏杀录音 → 已内置 WakeLock 申请屏幕常亮;浏览器切后台仍是天花板,
  口袋场景终局是硬件录音卡

**桌面端**
- winCodeSign 符号链接/下载超时/管理员 PATH/旧 exe 旧代码 四连坑（本文 §3.3）

**固件（ESP32 录音卡）**
- strap 脚 GPIO46/45/3;RTC_INT 非 RTC-IO 深睡叫不醒(改板建议);
  按键无外部上拉,深睡前必须切 RTC 域上拉;字库授权(§3.6 + firmware README)

**流程**
- BLE 协议改动必须 C/C# 同步 + 金标验证（§3.5）
- 录音卡交互改动:先改模拟器,再照搬固件状态机,改完跑 run_state_test.bat（§3.6/3.7）

---

## 6. 发布包与源码库的关系（给维护者）

**双工作区并行（2026-07-11 决策）**：`D:\claude\files-mentioned-by-the-user-chatgpt\`（Claude 用）
与 `D:\codex\files-mentioned-by-the-user-chatgpt\`（CODEX 用）长期并存，各含一对 源码+发布包。
守则：
1. 任一侧改完代码，先跑源码根目录的 `bash sync-check.sh`——有输出即漂移，
   以【测试通过 + 较新】一侧为准同步（避免双向同改同一文件）；
2. 同步后两侧各自验证：`python -m pytest -q`（server）+ `node --test`（web-client）；
3. 真源变更后**两侧都要** `bash export-release.sh` 重导发布包（改完文档也算变更，别漏）。

- 每个工作区内部：源码库 `ai-recorder-system/` 是真源；发布包 = 源码去掉开发专用内容的干净导出。
- 导出排除清单：`node_modules/`、`dist/`、`.venv/`、`__pycache__/`、`bin/ obj/ publish/`、
  `apps/desktop-client/app/client/`（UI 构建产物）、`server/data/` 运行时内容、
  网页端 demo 文件（`demo*.html/jsx`、`captionTest.jsx`、`caption-test.html`、
  `captionGrouping.js`、`stickToBottom.js`）、`docs/archive/`（历史文档）、
  固件 `tools/*.exe *.obj`。
- 同步方向铁律：**改代码改源码库 → 重新导出**；不要直接改发布包（会被下次导出覆盖）。
