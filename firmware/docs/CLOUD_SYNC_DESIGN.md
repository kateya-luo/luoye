# 落叶 v0.7.0 API v1 手动历史同步设计

适用固件：`v0.7.0-ui154-r2.11-syncqueue-r1`

设备 API：`luoye-device-api/1`

状态：`DEVICE + SERVER IMPLEMENTED / BOARD PENDING`

## 核心规则

- 每段录音都是独立会话，目录、会话 ID 和上传进度互不覆盖。
- 当前录音在线时读取最近一次 `fsync` 的安全边界并实时上传；闭合后继续完成尾片、MARK 和 final。
- 旧的已闭合历史会话默认不抢占网络；用户长按中间键进入同步页并确认后，才按最早优先 FIFO 上传。
- 活动会话只发送完整 160 KiB 分片；闭合后才发送不足一整片的尾部。
- 每个 160 KiB 分片在发送前计算 SHA-256。
- 云端 PCM 分片固定使用 `Content-Type: audio/L16;rate=16000;channels=1`；
  语音待办 WAV 仍使用 `audio/wav`。
- 分片携带字节 offset/count/SHA；ACK 的 `next_seq`、`received_samples` 和
  `acknowledged_bytes` 必须形成同一个连续确认点，并至少覆盖本次分片，否则拒绝推进。
- create、audio chunk、marks、final 各自使用稳定幂等键。
- `upload.state` 使用临时文件、`fflush + fsync`、备份和 rename 原子更新，并镜像到 `session.json`。
- `final_ack=true` 持久化后立即进入本地删除阶段；云端已经接管完整音频和处理任务。
- `marks.jsonl` 按物理行流式读取，每行使用稳定的 `mark-N` PUT；总文件可超过
  16 KiB。断网重启从首行幂等重放，单条损坏或超长记录会被记录并跳过，不阻塞音频收尾。
- 云端完整确认后自动删除对应本地 WAV 和会话目录。
- 网页可在任意时间下发删除命令，不要求录音先上传完成；如果目标仍在写入，命令留在服务器排队，WAV 安全闭合后设备自动重试执行。

## 工作流

```text
local_closed/recovered
  -> wait for middle-key sync confirmation
  -> create session (幂等)
  -> chunk 0..N (每片 ACK 后持久化)
  -> marks (幂等)
  -> final (幂等)
  -> persist final ACK
  -> atomic local delete
```

活动会话在 create/chunk 之间轮询 live revision，保证字幕时效。活动会话关闭并完成
云端 final 后才释放实时通道；手动历史同步随后从最早一场继续。设备重启后不存在
“前台会话”持久特权，所有未完成历史会话重新等待用户确认同步。

断网、超时、429 和 5xx 进入指数退避，3 秒起步、随机抖动、最大 5 分钟。
401/403 清除失效 Device Token、保留 WiFi 和所有本地会话，并自动生成新配对材料
回到认领流程；普通 409 与 400/413/422 等
协议错误停止该会话上传。只有 end 返回明确 `AUDIO_CHUNKS_MISSING` 才回退补片。

## 账号隔离

创建录音时把当前 `binding_generation` 固化进会话。未绑定时录制的会话不自动归属
后续账号；重新绑定后，旧 generation 的积压不会上传给新账号。

## 当前验收点

服务端接口已经实现。仍需真板注入断网、超时、重复分片、响应丢失、缺片、重启和解绑，
验证每种情况下服务端只有一份音频、设备断点不倒退、旧账号录音不泄漏，并且只有
final ACK 或明确网页删除命令能清除已闭合本地目录。
