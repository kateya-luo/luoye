# 落叶设备云端 API v1

状态：v1 契约冻结，ClearMeeting v0.13.0 / 落叶固件 v0.7.0 实现（2026-08-01）
适用范围：落叶固件、ClearMeeting 服务端、ClearMeeting Web Client

## 1. 设计目标

- 设备不保存账号密码；账号只在网页端登录。
- 一台设备同一时刻只归属一个账号，历史录音归属在会话创建时冻结。
- 解绑或转移设备时递增 `binding_generation`，旧令牌和旧代次请求立即失效。
- 录音、标记、结束、语音待办均支持断网重试和幂等重放。
- 固件只缓存近期议程与待办结果，服务器是账号数据的唯一权威来源。

## 2. 身份与令牌

### 2.1 网页账号令牌

网页使用现有用户名/密码登录。账号令牌只允许访问 `/api/v1/me/*` 与账号资源，不能充当设备令牌。

### 2.2 设备身份

设备使用 ESP32-S3 eFuse MAC 派生稳定的 `device_id`，格式为 `LY-AABBCCDDEEFF`。设备恢复出厂不会改变 `device_id`，只会清除当前配对与设备令牌。

### 2.3 配对凭据

设备开始配对时生成六位 `pairing_code` 与 128-bit `pairing_nonce`。服务器只保存其安全摘要并设置短期过期时间。网页只输入配对码；只有持有 `pairing_nonce` 的设备才能领取绑定后的设备令牌。

### 2.4 设备令牌

设备令牌是可撤销的随机 opaque token，服务器只保存其 SHA-256。同一账号内轮换 token 不增加 `binding_generation`；只有解绑或改绑账号才增加。所有设备业务请求同时校验：

- token 所属 `device_id`
- token 未撤销、未过期
- token 的 `binding_generation` 等于设备当前绑定代次
- URL 中的 `device_id` 与 token 一致

## 3. 绑定状态机

`unregistered -> pairing -> bound -> unbound -> pairing`

1. 设备调用 `POST /api/v1/device/pair/start` 登记自己生成的配对码与 nonce。
2. 网页登录后调用 `POST /api/v1/me/devices/claim`。
3. 服务器原子写入账号归属并递增 `binding_generation`。
4. 设备用 `pairing_nonce` 轮询 `POST /api/v1/device/pair/status`。
5. 状态变为 `bound` 时，设备一次性取得 device token。
6. 网页解绑后，所有现有 device token 立即撤销；历史会话仍属于创建它的原账号。

## 4. API 分区

- `/api/v1/auth/*`：网页登录与账号令牌。
- `/api/v1/me/*`：登录账号的设备、会议、议程与待办。
- `/api/v1/device/*`：设备配对、录音、议程同步与语音待办。
- v0.12.0 一次性移除旧 M1 `/api/devices/*` 与 `/device/*`；网页、服务端和落叶不保留双协议分支。

## 5. 首批接口

### 5.1 设备发起配对

`POST /api/v1/device/pair/start`

```json
{
  "device_id": "LY-AABBCCDDEEFF",
  "pairing_code": "123456",
  "nonce": "32-hex-characters",
  "firmware_version": "0.6.1",
  "hardware_revision": "LY-HW-ENG-20260710",
  "capabilities": ["fixed_sd", "pdm_stereo", "offline_upload", "agenda", "voice_todo"],
  "protocol_version": "luoye-device-api/1"
}
```

响应包含 `binding_status=pending`、`expires_at` 与 `poll_after_seconds`，不返回设备令牌。相同 `(device_id, nonce)` 重放必须返回相同配对状态。

### 5.2 网页认领设备

`POST /api/v1/me/devices/claim`

账号 Bearer token 鉴权，请求体：

```json
{"pairing_code":"123456","display_name":"我的落叶"}
```

认领操作必须在事务中完成；同一配对码只能成功一次。

### 5.3 设备领取绑定结果

`POST /api/v1/device/pair/status`

```json
{"device_id":"LY-AABBCCDDEEFF","nonce":"..."}
```

`pending` 响应不含敏感信息；`bound` 响应返回 `device_token`、`binding_generation`、脱敏账号与服务器时间。如果响应在网络中丢失，设备可以在配对记录过期前重试；服务器重新签发 token、撤销上一次由该配对记录签发的 token，但不能改变 `binding_generation`。

已绑定设备再次进入配对时，同一账号认领只轮换 token，不改变绑定代次；其他账号不能凭新配对码直接转移设备，必须先由原账号解绑。

### 5.4 网页设备管理

- `GET /api/v1/me/devices`
- `PATCH /api/v1/me/devices/{device_id}`
- `DELETE /api/v1/me/devices/{device_id}/binding`

## 6. 录音可靠上传

### 6.1 创建会话

`POST /api/v1/device/sessions`

设备提交 `client_session_id`、开始时间、音频格式与当前 `binding_generation`。v1 云端上传格式固定为 PCM S16LE、16 kHz、单声道；本地 WAV 可以继续保留双声道原件。请求必须带：

`Idempotency-Key: session:{client_session_id}:create`

服务器在首次创建时冻结 `owner_user_id`、`device_id` 和 `binding_generation`。重复请求返回相同 `server_session_id`。未绑定状态产生的本地录音不允许在设备后来绑定后自动归给新账号；它只保留在 SD 卡上。

### 6.2 上传分片

`PUT /api/v1/device/sessions/{server_session_id}/audio/{seq}`

请求携带 `X-Content-SHA256`、`X-Byte-Offset`、`X-Byte-Count`。服务器以 `(device_id, client_session_id, seq)` 去重，并返回连续确认点 `next_seq` 与 `acknowledged_bytes`。

### 6.3 标记与结束

- `PUT /api/v1/device/sessions/{id}/marks/{client_mark_id}`
- `POST /api/v1/device/sessions/{id}/end`

结束请求只有在服务器确认所有分片后进入 `processing`；存在缺片时返回 `missing_sequences`，设备继续补传。

### 6.4 状态与实时结果

`GET /api/v1/device/sessions/{id}/state?after_revision=N`

返回上传进度、字幕、翻译、处理状态和严格递增的 `revision`。网页读取同一会话数据，不另开旁路会话。

## 7. 议程与提醒

`GET /api/v1/device/agenda?after_revision=N&window_days=7`

服务器依据 device token 找到当前账号，不接受设备从请求体指定 owner。返回当前绑定代次、账号时区、服务器时间、事件、提醒和待办的完整滚动窗口。

设备只接受：

- `binding_generation` 与本机当前值一致
- `revision` 严格大于已缓存值

解绑或转移后立即清空设备议程缓存。

## 8. 语音待办

1. 设备生成稳定的 `client_todo_id` 并保存本地 WAV。
2. `PUT /api/v1/device/todos/{client_todo_id}/audio` 幂等上传。
3. 服务端完成 ASR 与结构化解析，生成账号私有 todo/event。
4. 设备轮询 `GET /api/v1/device/todos/{client_todo_id}/result?after_revision=N`。
5. 用户在设备上确认或取消，调用 `POST /api/v1/device/todos/{client_todo_id}/actions`。

解析结果允许 `due_at_utc=null`，表示识别出了待办但没有可靠时间。解析失败或超时时设备保留本地音频，可继续重试，不丢弃原始输入。

## 9. 错误与重试

统一错误结构：

```json
{
  "error": {
    "code": "BINDING_GENERATION_MISMATCH",
    "message": "设备绑定已变化",
    "retryable": false,
    "request_id": "req-..."
  }
}
```

- `400` 参数错误，不重试。
- `401` token 无效，回到配对状态。
- `403` 归属或绑定代次不匹配，不重试并清理账号缓存。
- `409` 只表示状态冲突或同一幂等键对应了不同请求内容；客户端不能把普通 409 当作成功，必须按错误码恢复。
- `422` 音频/内容校验失败，仅重传对应对象。
- `429/5xx` 指数退避并带抖动重试。

## 10. 第一阶段验收

- TEST1 认领设备后，TEST2 无法查看、改名、解绑或读取其任何数据。
- 配对码过期、重复认领与 nonce 错误均被拒绝。
- 解绑后旧 device token 立即返回 401/403。
- 重新绑定后 `binding_generation` 必须递增。
- 同一会话创建请求重复 20 次只产生一条会话记录。
- 同一音频分片重复上传只保存一份，SHA 不一致返回冲突。
- 服务器重启后绑定、上传确认点和议程 revision 全部可恢复。

## 11. 传输安全

局域网 HTTP 仅用于工程联调，不发送真实敏感录音。正式环境必须使用 HTTPS；正式固件默认拒绝明文 HTTP，工程固件通过显式构建开关临时允许。

## 12. v0.13.0 固定式 SD 管理

- `PUT /api/v1/device/storage/snapshot`：设备分页上报容量、本地会话及是否已经闭合，并取得策略及一条待执行命令。
- `POST /api/v1/device/storage/commands/{command_id}/ack`：设备回执完成、失败或拒绝及释放空间。
- `GET /api/v1/me/devices/{device_id}/storage`：当前账号读取自己设备的容量、会话和命令历史。
- `POST /api/v1/me/devices/{device_id}/storage/commands`：排队 `delete_sessions`、`delete_all_closed` 或 `cleanup_synced`。
- `PATCH /api/v1/me/devices/{device_id}/storage/policy`：设置自动清理、保留天数和空间阈值。

服务端只允许当前 owner 管理当前 `binding_generation`。`delete_sessions` 与 `delete_all_closed` 不检查上传完成状态；若目标仍在写入，设备不回执该命令，使其保持 pending 并在 WAV 安全闭合后自动重试。本地删除不级联删除云端会议。云端 final ACK 后设备会主动删除对应本地会话。
