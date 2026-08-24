# 落叶 v0.2.0 存储安全设计

适用版本：`0.2.0-dev.1`

## 不变量

1. 屏幕只有收到 `SESSION_CLOSE_DONE` 后才能显示“本地已保存”。
2. `STARTING`、`RECORDING`、`CLOSING` 期间不得开始第二段录音或进入深睡眠。
3. 任意 `fwrite`、`fflush`、`fsync`、`fclose` 失败都进入 `STORAGE_ERROR`，不能继续显示正在录音。
4. 上传器只能读取 `pcm_bytes_committed`，不能读取尚未同步到卡的数据。
5. 每次录音使用“设备 MAC + NVS 单调计数器 + 随机数”生成唯一会话 ID。

## 会话目录

```text
/sdcard/rec/<session-id>/
  session.json
  audio.wav
  marks.jsonl
  upload.state
```

会话创建成功的判据是四个文件均已创建并完成 `fflush + fsync`。`session.json`
以临时文件写入，再替换正式文件。

## 状态与收尾

```text
STANDBY -> STARTING -> RECORDING -> CLOSING -> ENDING -> STANDBY
                      |             |
                      +------ STORAGE_ERROR
```

- `STARTING`：创建会话目录和四个文件，写入可恢复的空 WAV 头。
- `RECORDING`：每 4 KiB 检查短写并刷新 C 库缓冲；每 64 KiB 更新 WAV 头并 `fsync`。
- `CLOSING`：停止麦克风、排空音频环形缓冲、修正 WAV 头、同步并关闭文件，
  原子更新 `session.json`。
- `ENDING`：只在上述步骤全部成功后显示“本地已保存”。
- `STORAGE_ERROR`：停止采集，保留可修复文件，显示具体错误并阻止新录音。

## 断电恢复

挂载 SD 后扫描 `/sdcard/rec/*`：

1. 按 `audio.wav` 实际长度对齐到完整 16-bit PCM 帧；
2. 截断半个样本，重写 44 字节 WAV 头并同步；
3. 将 `marks.jsonl` 截到最后一个换行，移除断电造成的半条 JSON；
4. 将未闭合会话标为 `recovered`，记录修复次数和关闭原因；
5. 保留 `upload.state`，供后续版本断点上传。

## v0.2.0 真板验收

- 录音中拔卡：界面立即退出“录音中”，且不能显示“已保存”。
- 卡写满：已有 WAV 可播放，最后一个完整样本不被破坏。
- 录音中复位/断电：重启自动修复，修复后的 WAV 可播放。
- 连续录音 2 小时：音频丢弃计数为 0；若非 0，日志和会话元数据必须可见。
- 低电临界：停止麦克风、排空缓冲并完成同步后才允许关机。
