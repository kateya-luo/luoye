# 落叶设备 API/2

适用版本：ClearMeeting `1.0.1`、落叶固件 `V2.0.0/V2.0.1`（兼容旧固件）。契约标识为
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

设备先调用 `POST /api/v2/device/sessions` 创建会议，再上传可变长度、偶数字节的
PCM 分片。V2.0.0 使用 32 KiB，旧固件的 160 KiB 保持兼容：

`PUT /api/v2/device/sessions/{server_id}/audio/{seq}`

请求携带绝对 `X-Byte-Offset`、长度和 SHA-256。服务器分别返回：

- `acknowledged_bytes`：从文件开头连续完整的权威进度；
- `live_acknowledged_bytes`：当前实时 epoch 的最新进度。

两条游标分离，保证网络恢复后实时字幕不需要等待旧缺口补完。
服务器在音频文件 fsync 并提交 SQLite 游标后立即返回 ACK；每场会议的后台
实时 ASR 队列再严格按 `seq` 串行消费，FunASR 处理不占用上传 HTTP 请求。

V2.0.0 可继续使用兼容查询：

`GET /api/v2/device/sessions/{id}/state?after_revision=N&include_partial=1&after_display_revision=M`

读取最新变化字幕。`partial` 只保存当前假设，不进入正式字幕、会议历史、纪要、
翻译、待办或声纹流程；正式结果仍由 `revision`/`captions` 表示。旧固件未携带
`include_partial=1` 时，响应字段和正式字幕语义保持不变。

V1.0.1 修正了两类时序问题：FunASR `2pass-online` 的增量小片段会在服务器内
累计为当前完整句；一个读取批次内“上一句 final + 下一句 partial”会严格按收到
顺序落库，不再由 final 清掉下一句。服务器同时把累计 partial 推送给网页旁听
通道，网页端以 `partial_replace=true` 原位替换，不进行字符串追加。

### 2.1 V2.0.1 墨水屏推荐状态查询

```text
GET /api/v2/device/sessions/{id}/state
  ?after_revision=R
  &include_partial=1
  &after_display_revision=D
  &after_caption_revision=C
  &after_speaker_revision=S
  &after_translation_revision=T
  &after_summary_revision=M
```

V1.0.1 保留旧 `revision`，并增加独立频道游标：

- `caption_revision`：正式字幕文字新增或修订；
- `speaker_revision`：说话人标签更新；
- `translation_revision`：译文更新；
- `summary_revision`：纪要/时间线更新；
- `display_revision`：录音卡显示事件。

游标是同一全局单调序列上的事件戳，保证重启后不倒退，但不同频道内部允许跳号。
响应中的 `caption_changed`、`speaker_changed`、`translation_changed` 和
`summary_changed` 应分别驱动对应区域，不能再用全局 `revision` 重绘字幕。

`caption_updates` 和兼容字段 `captions` 都是以 `seg_id` 为主键的 upsert：新 ID
追加，已有 ID 原位更新。声纹完成后只出现在 `speaker_updates`，不会把同一句文字
再次放入 `captions`。所有正式字幕、译文和 partial 的设备响应上限统一为 512 个
UTF-8 字节，截断不会切断汉字。

落叶墨水屏把事件传输、设备快照和物理刷新视为三个不同颗粒度：服务器仍按事件
更新游标，设备按 `seg_id` 消费正式字幕资源，但下方实时区只显示查询时最新的累计
`partial` 快照。墨水屏约 1.5 秒最多刷新一次，同一窗口内的多个 partial 不逐条回放，
否则设备会追赶旧文本并增加残影和显示延迟。上一句 final 与下一句 partial 可以在
同一响应中分别通过 `caption_updates` 和顶层 `partial` 同时交付。

### 2.2 可选的可靠显示事件

`display_events` 面向需要逐事件恢复的高刷新率客户端，不是落叶 V2.0.1 墨水屏的
必需路径。客户端显式携带 `include_display_events=1` 后，响应最多按升序返回 4 个
`display_events`：

```json
{
  "display_revision": 93,
  "display_events": [
    {"display_revision": 90, "kind": "partial", "text": "现在我们讨论", "seg_id": null},
    {"display_revision": 91, "kind": "final", "text": "现在我们讨论上传速度。", "seg_id": "dev-..."},
    {"display_revision": 92, "kind": "partial", "text": "接下来", "seg_id": null},
    {"display_revision": 93, "kind": "clear", "text": "", "seg_id": null}
  ],
  "display_events_through_revision": 93,
  "display_events_pending": false,
  "display_events_truncated": false
}
```

选择该模式的客户端必须逐条处理并把下一次 `after_display_revision` 设置为
`display_events_through_revision`。当 `display_events_pending=true` 时立即继续查询，
不能直接跳到响应顶层的最新 `display_revision`。服务器保留每场会话最近 256 个未
确认事件；若 `display_events_truncated=true`，客户端应以当前 `partial` 和正式
`caption_updates` 恢复屏幕快照，再把游标追到当前值。

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
