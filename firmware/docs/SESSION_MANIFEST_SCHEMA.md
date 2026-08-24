# 落叶录音会话 Manifest 规范

Schema：`luoye-session/1-draft`
状态：设备端由 `v0.6.1-cloud-v1` 继续读写；API v1 端到端联调待验收。

每个录音目录必须独立拥有 `session.json`。它是录音恢复、后台补传和账号归属的本地
真源，不能只把状态保存在 RAM。

## 1. 示例

```json
{
  "schema": "luoye-session/1",
  "client_session_id": "LY-20260730-210000-a1b2c3d4",
  "server_session_id": null,
  "device_id": "pending-device-id",
  "binding_generation": 1,
  "scene": "meeting",
  "title": null,
  "started_at_utc": null,
  "ended_at_utc": null,
  "source_language": "zh-CN",
  "target_language": null,
  "audio": {
    "path": "audio.wav",
    "codec": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "pcm_bytes_committed": 0,
    "wav_closed": false,
    "sha256": null
  },
  "upload": {
    "state": "queued",
    "remote_session_created": false,
    "next_seq": 0,
    "acknowledged_bytes": 0,
    "final_ack": false,
    "marks_acked": false,
    "retry_count": 0,
    "last_http_status": 0,
    "result_revision": 0,
    "result_pcm_bytes": 0,
    "last_error": null
  },
  "marks": {
    "path": "marks.jsonl",
    "count": 0,
    "uploaded_count": 0
  },
  "recovery": {
    "close_reason": null,
    "repair_count": 0,
    "last_repaired_at": null
  }
}
```

## 2. 状态

`upload.state` 是上传进度的原子真源，`session.json.upload` 是同内容的可读镜像。
`v0.5.0` 写入 `luoye-upload/2`，在 v1 字段基础上增加 `result_revision` 和
`result_pcm_bytes`；缺失字段按 0 迁移。
状态只能按下列方向迁移：

```text
recording
  → closing
  → local_closed
  → queued
  → uploading
  → awaiting_final
  → final_acked
  → local_deleted
```

故障可以进入 `local_error`、`upload_retry` 或 `auth_blocked`，但不得删除本地会话。
不可重试的协议错误可进入 `permanent_error`，需由兼容固件或人工迁移后恢复。

## 3. 原子更新

更新 `session.json` 必须：

1. 写入同目录的 `session.json.tmp`。
2. `fflush`。
3. `fsync`。
4. 校验完整 JSON 和 schema。
5. 原子替换正式文件。
6. 目录项同步能力受 FATFS 限制时，保留上一代 `session.json.bak`。

启动恢复顺序：

1. 扫描所有会话目录。
2. 优先读取有效 `session.json`，无效时尝试 `.bak`。
3. 对未闭合 WAV 根据实际文件长度修复 WAV 头。
4. `acknowledged_bytes` 不得超过实际 PCM 长度。
5. 所有未完成会话重新进入队列，不只恢复最近一次会话。

## 4. ID 与幂等

- `client_session_id` 在设备本地永久唯一；RTC 无效时也不能复用。
- 会话创建幂等键：`session:{client_session_id}:create`。
- 分片幂等键：`session:{client_session_id}:audio:{seq}:{sha256}`。
- 标记幂等键：`session:{client_session_id}:mark:{client_mark_id}`。
- final 幂等键：`session:{client_session_id}:end`。
- 换绑后旧会话保留创建时的 `binding_generation`，不得自动转移到新账号。

## 5. 兼容与迁移

- 未识别的高版本 schema 不得被低版本固件改写。
- 新增字段默认向后兼容，删除或改语义必须提升 schema 主版本。
- release manifest 必须记录可读写的 session schema 范围。
- 当前录音在线时实时上传；已闭合历史会话只在用户从中间键确认同步后按最早优先上传。
- 云端 final ACK 持久化后立即删除该本地会话；若在删除中断电，`delete.safe` 标记用于重试原子收尾。
- 当前绑定账号可随时从网页请求删除本地会话，不以 `final_acked` 为前置条件；正在写入时命令保持 pending，安全闭合后自动执行。
- 活动录音期间 `upload.state` 可以更新；`session.json` 仍由录音 writer 管理，避免
  两个任务同时 rename manifest。会话闭合后再镜像上传状态。
