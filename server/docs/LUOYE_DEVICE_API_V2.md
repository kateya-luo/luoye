# 落叶设备 API/2

适用版本：ClearMeeting `0.15.0`、落叶固件 `0.8.2`。契约标识为
`luoye-device-api/2`，设备接口统一位于 `/api/v2`。

ClearMeeting v0.15.0 保持设备协议兼容，并在服务器内部把设备事项同步映射到统一事项模型。
设备上传协议和固件请求格式没有变化；网页通过现有活动会话和旁听 WebSocket
接收字幕与 `meeting_update`，因此 v0.8.2 固件无需重新烧录。

## 1. 音频和认证

- PCM S16LE、16 kHz、单声道、16 bit；一个采样 2 字节。
- 设备请求携带 `X-Luoye-Protocol`、固件版本和设备 ID。
- 配对后使用与账号、设备和 `binding_generation` 绑定的设备 Bearer token；解绑后旧 token 立即失效。
- 创建会话、实时分片、MARK、切换实时 epoch、延迟补洞和完成请求均支持幂等重放。

## 2. 在线录音

设备先调用 `POST /api/v2/device/sessions` 创建会议，再按 160 KiB 调用：

`PUT /api/v2/device/sessions/{server_id}/audio/{seq}`

请求携带绝对 `X-Byte-Offset`、长度和 SHA-256。服务器分别返回：

- `acknowledged_bytes`：从文件开头连续完整的权威进度；
- `live_acknowledged_bytes`：当前实时 epoch 的最新进度。

两条游标分离，保证网络恢复后实时字幕不需要等待旧缺口补完。

## 3. 录音中断网和恢复

断网期间录音始终继续写入 SD。恢复网络且录音仍在进行时，设备调用：

`POST /api/v2/device/sessions/{server_id}/live-resume`

请求给出缺口开始字节和当前录音字节。服务器登记新的实时 epoch，设备立即上传当前声音；断网期间的旧音频仍留在 SD，不占用实时通道追赶。

录音停止时，如存在缺口，设备调用：

`POST /api/v2/device/sessions/{server_id}/defer`

服务器将会议置为 `awaiting_repair` 并返回缺失范围。服务器重启恢复会跳过该状态，不会在用户同步前提前结束会议。

## 4. 手动同步和 10 MiB 补洞

用户长按中间键进入同步页，再单击中间键确认。固件调用 `upload-plan` 获取缺失范围，并以最大 10 MiB 的逻辑范围调用 `audio-range`。服务器按绝对字节偏移重建完整 PCM，再按时间轴识别并插入断网遗漏内容。没有 2 MiB 自动降级。

全部范围、MARK 和元数据到齐后调用 `complete`。只有服务器验证从字节 0 到文件末尾无缺口，会议才进入后台处理。

## 5. SD 删除完全独立

网页端的单项删除和一键删除全部，仅向对应录音卡下发本地删除命令：

- 不调用会议 `cancel`；
- 不删除云端会议、字幕、纪要、待办或音频；
- 不检查上传状态、会议状态或录音年龄；
- 正在录音的文件由固件等到安全收尾后再执行，本地删除结果通过命令 ACK 返回。

因此，网页“会议历史删除”和“设备 SD 删除”是两套互不关联的业务。

## 6. 关键端点

| 用途 | 方法与路径 |
|---|---|
| 兼容检查 | `GET /api/v2/build-info` |
| 配对 | `POST /api/v2/device/pair/start`、`pair/status` |
| 创建录音 | `POST /api/v2/device/sessions` |
| 在线分片 | `PUT /api/v2/device/sessions/{id}/audio/{seq}` |
| 恢复当前实时音频 | `POST /api/v2/device/sessions/{id}/live-resume` |
| 结束并等待补洞 | `POST /api/v2/device/sessions/{id}/defer` |
| 获取补洞计划 | `POST /api/v2/device/sessions/{id}/upload-plan` |
| 上传 10 MiB 范围 | `PUT /api/v2/device/sessions/{id}/audio-range` |
| 补洞完成 | `POST /api/v2/device/sessions/{id}/complete` |
| SD 清单和命令 | `/api/v2/device/storage/*`、`/api/v2/me/devices/*/storage*` |

## 7. 调度优先级

录音写 SD 独立于网络任务。云请求同一时刻最多执行一个，优先级为：在线音频/字幕、录音收尾、用户手动同步、语音待办、议程、SD 清单与删除命令。失败任务只记录自己的重试时间，不以长延时阻塞整个调度器。
