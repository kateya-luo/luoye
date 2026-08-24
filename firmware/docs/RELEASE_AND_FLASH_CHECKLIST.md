# 落叶固件构建、烧录与发布门禁

> 适用项目：落叶（Luoye）Recorder Card
> 核心规则：**未达到 `READY_TO_FLASH` 的版本不烧录；未通过真板验收的版本不标记完成。**

## 1. 构建类型

| 类型 | 版本示例 | 允许用途 | 禁止用途 |
|---|---|---|---|
| Dev | `v0.2.0-dev.3` | 开发板调试、专项验证 | 交付、量产 |
| RC | `v0.2.0-rc.1` | 完整版本验收 | 量产 |
| Accepted | `v0.2.0` | 下一版本开发基线 | 对外正式发布 |
| Release | `v1.0.0` | 正式交付和生产 | 未审批变更 |

## 2. READY_TO_FLASH 前置检查

### 源码

- [ ] 当前工作树无来源不明的修改。
- [ ] 当前 commit 已记录。
- [ ] `PROJECT_VER` 与计划烧录版本一致。
- [ ] 本版本目标清单已全部完成。
- [ ] 不包含 WiFi 密码、账号密码、Token、私钥或生产证书。
- [ ] `board_pins.h` 与目标 PCB 硬件版本一致。
- [ ] 分区表变更已经单独评审。
- [ ] NVS/manifest schema 变更有迁移与回滚方案。

### 自动测试

- [ ] 状态机 PC 回归测试通过。
- [ ] 本版本新增单元测试通过。
- [ ] API 协议测试向量通过。
- [ ] 静态检查和编译警告检查通过。
- [ ] 若修改存储：断电恢复测试通过。
- [ ] 若修改上传：重复 chunk/final 幂等测试通过。
- [ ] 若修改账号：账号隔离和 Token 吊销测试通过。
- [ ] 若修改 OTA：升级失败和自动回滚测试通过。

### 干净构建

在“ESP-IDF 5.5 CMD/PowerShell”中执行：

```powershell
cd /d D:\OPENOP\recorder-card-hw-test\firmware\recorder-card
idf.py set-target esp32s3
idf.py fullclean
idf.py build
```

- [ ] 使用路线图指定的 ESP-IDF 版本。
- [ ] `idf.py build` 返回 0。
- [ ] 记录 app、IRAM、DRAM、Flash 和分区占用。
- [ ] 检查没有越过分区或内存预算。
- [ ] 保存完整 build log。

## 3. 产物归档

每个候选版本建立：

```text
releases/<version>/
  recorder_card.bin
  bootloader.bin
  partition-table.bin
  assets.bin
  flash_args
  build.log
  serial-COMxx.log
  manifest.json
  TEST_REPORT.md
  RELEASE_NOTES.md
```

`manifest.json` 至少记录：

```json
{
  "version": "v0.2.0-rc.1",
  "git_commit": "<40-char sha>",
  "idf_version": "v5.5.4",
  "hardware_revision": "<pcb revision>",
  "build_utc": "<ISO-8601>",
  "app_sha256": "<sha256>",
  "bootloader_sha256": "<sha256>",
  "partition_sha256": "<sha256>",
  "assets_sha256": "<sha256>",
  "sdkconfig_sha256": "<sha256>",
  "api_contract_version": "<contract version>",
  "server_release_id": "<server release>",
  "minimum_client_version": "<app version>"
}
```

- [ ] 所有文件存在。
- [ ] 所有 SHA-256 已重新计算并写入 manifest。
- [ ] manifest 的 commit 与源码一致。
- [ ] API contract、服务器 release、数据库 migration 和客户端兼容范围已记录。
- [ ] 上一个 accepted 版本 release 包仍完整可用。

## 4. 烧录前检查

- [ ] 确认目标板编号、PCB 版本和芯片型号。
- [ ] 确认串口号，不能凭上一次 COM 号假定。
- [ ] 关闭占用串口的 monitor、串口助手和其它程序。
- [ ] 确认目标是开发板、RC 板还是量产板。
- [ ] 保存当前 accepted 版本的回滚产物。
- [ ] 分区表或 NVS schema 变化已明确是否需要擦除。
- [ ] 普通版本禁止执行 `erase_flash`，除非版本说明明确要求且用户批准。
- [ ] 普通烧录禁止执行任何 eFuse 命令。

推荐先验证芯片连接：

```powershell
esptool.py --chip esp32s3 -p COMxx chip_id
```

烧录候选版本：

```powershell
cd /d D:\OPENOP\recorder-card-hw-test\firmware\recorder-card
idf.py -p COMxx flash monitor
```

退出 monitor：

```text
Ctrl + ]
```

## 5. 烧录后通用冒烟测试

- [ ] 串口打印的版本、commit、硬件版本与 manifest 一致。
- [ ] Reset reason 符合预期，无 boot loop 或 panic。
- [ ] Flash、PSRAM 初始化正常。
- [ ] 红、黄、绿 LED 状态正确。
- [ ] 墨水屏方向、镜像和文字正确。
- [ ] REC、MARK、BACK、BOOT 按键正常。
- [ ] BQ25186、MAX17048、PCF8563 正常。
- [ ] RTC 时间与电池电量合理。
- [ ] 双麦能够采集且左右输入均非固定值。
- [ ] SD 可挂载、写入、关闭和播放 WAV。
- [ ] WiFi 和服务器状态符合本版本能力。
- [ ] 无明显任务栈、内存、看门狗或 BUSY 超时错误。

## 6. 版本专项测试

每个版本必须执行路线图对应的真板验收；以下故障测试不得省略：

### 存储

- [ ] 录音时拔卡。
- [ ] 卡满/短写。
- [ ] close/fsync 前后复位。
- [ ] 启动时无卡、启动后插卡。
- [ ] 新旧 SD 卡 volume identity 不同。

### 网络和上传

- [ ] WiFi 断开/恢复。
- [ ] DNS、TLS、超时和 5xx。
- [ ] 401、403、409、413、429。
- [ ] chunk 已收但 ACK 丢失。
- [ ] final 已收但 ACK 丢失。
- [ ] 旧会话未完成又创建新会话。

### 电源

- [ ] 低电量告警。
- [ ] 临界电量安全收尾。
- [ ] USB 插拔和充满。
- [ ] 录音中请求关机。
- [ ] RTC 和按键深睡唤醒。

### 账号和隐私

- [ ] 账号 A/B 数据隔离。
- [ ] 设备解绑后旧 Token 失效。
- [ ] 换绑时旧 backlog 不上传给新账号。
- [ ] 删除/保留策略符合产品规则。

## 7. 判定规则

### 允许标记 BOARD_PASS

- 通用冒烟测试全部通过。
- 本版本专项测试全部通过。
- 串口无未解释的 panic、WDT、SD 写错和协议错误。
- 所有测试证据已保存。

### 必须判定失败并回滚

- 版本/commit 与 manifest 不一致。
- 启动循环、panic 或看门狗复位。
- 录音界面显示正常但 WAV 未写入。
- 会话未闭合却允许关机或新录音。
- 服务器 ACK 未确认却删除本地音频。
- 账号之间可看到对方会议。
- TLS 失败后降级为 HTTP。
- OTA 失败后无法回到旧版本。

回滚后：

- [ ] 记录失败版本和复现步骤。
- [ ] 保留失败串口日志和产物，禁止覆盖。
- [ ] 状态退回 `IN_PROGRESS`。
- [ ] 修复后增加 `dev.N` 或 `rc.N`，不得复用同一版本号。

## 8. 单次烧录记录模板

```text
烧录时间：
操作者：
设备编号：
PCB版本：
串口：
固件版本：
Git commit：
ESP-IDF版本：
构建类型：Dev / RC / Accepted / Release
是否修改分区：
是否擦除NVS：
App SHA-256：

烧录命令：
烧录结果：
启动结果：
冒烟测试：
专项测试：
串口日志路径：
发现问题：
回滚版本：
最终结论：FLASHED / BOARD_PASS / FAILED / ACCEPTED
```

## 9. eFuse 安全红线

下列命令或动作不属于普通固件烧录，必须单独批准：

- Secure Boot key digest 烧录。
- Flash Encryption Release 模式启用。
- 禁用 UART 下载、USB 下载或 JTAG。
- Anti-rollback `secure_version` 推进。
- 任何 `espefuse.py burn_*` 操作。

开发阶段只准备配置、镜像和验证流程，不执行不可逆烧录。
