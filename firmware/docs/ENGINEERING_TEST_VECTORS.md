# 落叶 v0.6.1-cloud-v1 Engineering 测试向量

目标：证明任何一个烧录包都能追溯到唯一源码，并能从串口判断各子系统初始化结果。

## 1. 自动门禁

| ID | 输入 | 预期 |
|---|---|---|
| `ENG-001` | `tools/run_state_test.bat` | 退出码 0，现有交互断言全部通过 |
| `ENG-002` | `build_profile.ps1 -Profile dev` | app descriptor=`0.6.1-cloud-v1`，target=`esp32s3` |
| `ENG-003` | `build_profile.ps1 -Profile rc -FullClean` | 无编译错误；产物地址来自 `flasher_args.json` |
| `ENG-004` | dirty Git 工作树运行打包 | 必须拒绝 |
| `ENG-005` | 版本不一致运行打包 | 必须拒绝 |
| `ENG-006` | 正确 RC 构建运行打包 | 生成 flash/symbols ZIP、manifest 和 SHA-256 sidecar |
| `ENG-007` | 解压两个 ZIP | 所有 `SHA256SUMS.txt` 校验通过 |
| `ENG-008` | 源码敏感词扫描 | 不得存在私钥、生产证书、真实 WiFi 密码或 Token |
| `ENG-009` | `tools/run_provisioning_test.bat` | 表单解码、UTF-8 SSID 和 WiFi 长度边界通过 |
| `ENG-010` | `tools/run_provisioning_static_checks.ps1` | 固定 HTTPS、无账号字段、无插拔卡文案 |
| `ENG-011` | `tools/run_upload_protocol_test.bat` | 分片边界、ACK、幂等键、HTTP 分类与退避通过 |
| `ENG-012` | `tools/run_cloud_sync_static_checks.ps1` | 持久字段、全会话扫描、SHA/幂等请求头和本地保留策略通过 |
| `ENG-013` | `tools/run_live_protocol_test.bat` | revision、连续偏移、UTF-8、文本长度和查询游标边界通过 |
| `ENG-014` | `tools/run_live_ui_static_checks.ps1` | 活动会话、安全边界、会议/翻译页面和 15 秒刷新门禁通过 |
| `ENG-015` | `tools/run_agenda_protocol_test.bat` | UTF-8、revision、账号代际、最近提醒和查询游标通过 |
| `ENG-016` | `tools/run_agenda_todo_static_checks.ps1` | 原子缓存、RTC、待办旁路、账号隔离和全 FAST 刷新门禁通过 |
| `ENG-017` | dev + HTTP origin 但无 `-AllowInsecureHttp` | 构建脚本必须拒绝 |
| `ENG-018` | rc/release + HTTP origin | 即使传开关也必须拒绝 |
| `ENG-019` | 普通 409 注入 | create/chunk/mark/action 均不得推进成功状态 |
| `ENG-020` | `TODO_REVISION_MISMATCH` | action 回到 result 轮询，不标记 created/cancelled |

## 2. 启动日志

每次启动必须出现且内容一致：

```text
LY|BOOT|product=Luoye version=0.6.1-cloud-v1 ... commit=<12hex> dirty=0 ...
LY|HW|... flash=16777216 psram=8388608 ...
LY|COMPAT|api=luoye-device-api/1 ...
LY|INIT|subsystem=nvs ...
LY|INIT|subsystem=event_queue ...
LY|INIT|subsystem=timers ...
LY|INIT|subsystem=led ...
LY|INIT|subsystem=ui ...
LY|INIT|subsystem=keys ...
LY|INIT|subsystem=power_i2c ...
LY|INIT|subsystem=storage_sd ...
LY|INIT|subsystem=audio_pdm ...
LY|INIT|subsystem=agenda_todo ...
LY|INIT|subsystem=network ...
LY|BOOT_READY|ok=... degraded=... failed=... event_drops=0
```

错误码：

| 范围 | 子系统 |
|---|---|
| `LYE-100` | NVS |
| `LYE-110`～`LYE-120` | 事件队列和状态机定时器 |
| `LYE-200`～`LYE-220` | LED、屏幕、按键 |
| `LYE-300` | 电源与 I²C |
| `LYE-310` | SD |
| `LYE-320` | PDM 音频 |
| `LYE-330` | 网络 |

## 3. 故障向量

| ID | 操作 | 预期 |
|---|---|---|
| `ENG-HW-001` | 软件注入存储不可用 | 显示“存储不可用/录音未保存”，不提示插拔卡 |
| `ENG-HW-002` | 无 WiFi 凭据启动 | network 初始化可完成，业务状态保持离线 |
| `ENG-HW-003` | 断开墨水屏启动 | `ui=FAILED, code=LYE-210` 或刷新错误；其余日志仍可读 |
| `ENG-HW-004` | I²C 总线器件不可达 | `power_i2c` 错误可定位，不打印密码/Token |
| `ENG-HW-005` | 麦克风初始化失败 | `audio_pdm=FAILED, code=LYE-320`，按 REC 拒绝假录音 |
| `ENG-HW-006` | 注入满事件队列 | `LY|EVENT_DROP` 计数增长并限频报警 |
| `ENG-HW-007` | 无 WiFi 凭据长按 BACK 3 秒 | 出现无需密码的 `LUOYE-XXXX` 开放 SoftAP 和 `192.168.4.1` |
| `ENG-HW-008` | 提交错误 WiFi 密码 | 不覆盖 NVS 中已验证的旧凭据；页面可重试 |
| `ENG-HW-009` | 提交正确 WiFi | 获得 IP 后才写 NVS，复位后自动重连 |
| `ENG-HW-010` | 服务器 pair API 不可达 | 显示“服务器不可用”，本地录音正常 |
| `ENG-HW-011` | 账号 A Claim，账号 B 查询 | A 可见设备，B 不可见；需要服务器联调 |
| `ENG-HW-012` | 离线连续录制 A、B 后重启联网 | 两个目录均保留，补传任务依次发现两个会话 |
| `ENG-HW-013` | 每个分片 ACK 前断网/复位 | `acked_pcm_bytes` 不前进，恢复后用相同 seq/offset/SHA 重发 |
| `ENG-HW-014` | 服务端收到分片但丢弃响应 | 重发命中同一幂等键，服务端音频只拼接一次；需要服务器联调 |
| `ENG-HW-015` | 恰好录制 160 KiB PCM | 分片 ACK 后仍单独发送 final，最终 `final_acked=true` |
| `ENG-HW-016` | 上传完成 | 固定式 SD 中 WAV、marks 和 manifest 仍存在 |
| `ENG-HW-017` | 活动录音不足 160 KiB | 不上传不稳定尾片；达到完整分片后才发送 |
| `ENG-HW-018` | 服务端返回旧 revision、回退偏移或错误会话 ID | 拒绝结果，屏幕保留上一有效 revision |
| `ENG-HW-019` | 翻译返回同一 revision 原文/译文 | 译文页和双语页内容一致，BACK 三页循环 |
| `ENG-HW-020` | 在线录音时断网再恢复 | 离线页保持录音；恢复后从已持久化 revision 继续 |
| `ENG-HW-021` | 连续字幕回屏 4 小时 | 无镜像、错位、严重残影、栈溢出或持续内存下降 |
| `ENG-HW-022` | 缓存未来提醒后断网关机 | 使用 light sleep；PCF8563 到点唤醒并显示正确标题 |
| `ENG-HW-023` | 提醒页按 MARK | 10 分钟后再次提醒，RTC 只编程一次，不提前/重复触发 |
| `ENG-HW-024` | 按住 MARK 说“明晚七点学生会” | 本地生成单声道 WAV；联网后出现确认页，REC 确认后才显示已创建 |
| `ENG-HW-025` | 待办录音后立即断网/复位 | todo sidecar 保持 queued；恢复后相同幂等键上传且只创建一次 |
| `ENG-HW-026` | 账号 A 留下待办后解绑并绑定 B | B 不显示、不上传、不确认 A 的待办或议程 |

## 4. 真板验收

- 同一 RC 连续复位 20 次，version/commit/hardware/IDF/ELF SHA 前缀一致。
- 开机后墨水屏设备状态页显示 `0.6.1-cloud-v1`（空间不足的页面可显示 `0.6.1`）。
- REC、MARK、BACK、BOOT 行为与 baseline 一致。
- 固定式 SD 录音 10 分钟并播放；左右麦、存储和墨水屏不回归。
- 使用软件故障注入验证存储不可用、写失败、满卡和关闭失败。
- 保存完整串口日志、COM 口、板号、硬件版本和操作者结论。

未完成上述真板测试时，版本状态只能是 `READY_TO_FLASH`，不能标记 `ACCEPTED`。
