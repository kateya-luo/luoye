# ClearMeeting 双通道 + 离线补洞 架构设计

> 状态：草案 v0.1（2026-06-30）
> 目标读者：本项目维护者
> 关联：[[clearmeeting-reconnect-policy]]、`protocol/README.md`(录音卡 BLE 协议)、`server/app/post_meeting_diarizer.py`

---

## 1. 背景与问题

当前所有音频都走**单一实时流**（WebSocket → FunASR `2pass-online`），这条流式通道同时承担两个互相冲突的职责：

1. **现场字幕**（要低延迟）
2. **音频可靠留存 + 最终转写质量**（要完整、要准）

由此暴露的真实问题：

- **断网丢字**：网络中断期间，客户端把积压音频在重连后**瞬时倾倒**给流式 ASR，FunASR 在线解码器吃不消 → 吐乱码（`呃/用f/器器我们`），音频字节到了（时间戳从 00:12 跳到 00:42）但**文字丢了**。
- **限速补传是 band-aid**：把倾倒改成限速喂（180ms/帧）只是缓解，本质还是"用流式工具处理批量音频"，不可靠且无法扩展。
- **录音卡批量上传扛不住**：录音卡断网 10 分钟后重连、或一次性上传 2h 录音，走流式通道从架构上就是错的——流式模型只能按接近实时的速度处理，2h 音频要 ≥2h CPU 时间。

**典型场景（驱动本设计）**：录音卡用手机热点联网，用户接电话 10 分钟导致热点断开，录音卡本地继续录（存 flash），电话结束重连。期望：断网那 10 分钟的音频补传上来、离线转写、**按时间原位插回字幕**、并刷新滚动纪要。

---

## 2. 目标

- **零丢字**：只要音频被采集，最终一定出现在转写里（不依赖实时流是否完整）。
- **现场可用**：实时字幕作为"现场预览"，断网/补传期间体验可降级但不阻塞。
- **一套机制覆盖三场景**：实时客户端断网容错、录音卡断网补传、录音卡 2h 批量上传。
- **时间正确**：补传内容按会议时间**原位插入**，不是 append 到末尾。
- **可演进**：先 CPU 跑通，再上 GPU（GTX 1060）提速，不改架构。

### 非目标（本期不做）

- 实时（流式）说话人最终对齐——沿用现有"会议结束全局聚类"。
- 多会议并行离线转写的调度优化——先单队列。
- 端到端加密音频留存。

---

## 3. 核心架构：双通道

```
                            ┌─────────────────────────── 后端 ───────────────────────────┐
录音源(录音卡/浏览器)        │                                                              │
   │                        │   ┌─ FunASR online ─→ 实时分段(provisional)                  │
   ├── A. 实时流(尽力而为) ──┼──▶│                         │                                │
   │   低延迟、可丢、可乱     │   └─────────────────────────┼──→ 会议时间轴(Timeline)        │
   │                        │                              │      (按 offset 排序的分段集)  │
   │                        │                              ▲                                │
   └── B. 可靠音频同步 ──────┼──▶ 完整 .pcm/.adpcm ─→ FunASR offline ─→ 补洞分段(authoritative)│
       带seq+时间偏移        │   (落盘，断点续传)            │                                │
       断点续传+ack          │                              └──→ 滚动纪要增量重算            │
                            └──────────────────────────────────────────────────────────────┘
```

- **通道 A（实时流）**：现有 WS。职责仅剩"现场字幕预览"。允许断网丢段、允许乱码。**不再是数据可靠性的依赖**。
- **通道 B（可靠音频同步）**：新增。把原始音频带**序号 + 会议时间偏移**可靠地传到后端（断点续传 + ack），后端拼出完整音频文件。这是**真相之源**。
- **离线转写**：通道 B 的音频（整段或某个时间区间）交给 **FunASR offline**（VAD 切句 + ASR + 标点），产出带时间戳的权威分段。
- **合并**：权威分段按时间偏移**插入/替换** Timeline 中对应区间 → 推送客户端 → 字幕原位更新 → 重算滚动纪要。

### 为什么是两条通道而不是"加固实时流"

加固实时流（加 seq + 服务端缺口检测 + 客户端重传）会把"低延迟预览"和"可靠留存"继续耦合，且仍受流式模型限制。拆开后：A 专心低延迟、B 专心可靠、离线专心质量，各自单一职责，且 B + 离线天然适配录音卡批量场景。

---

## 4. 时间锚定模型（地基）

整个方案成立的前提：**每一段音频/字幕都带"会议时间偏移 offset_ms"**，Timeline 按 offset 排序，而不是按到达顺序 append。

- **offset 来源**：
  - 录音卡：BLE 帧已有 `sequence`（20ms/帧），`offset_ms = sequence * 20`。设备本地连续计数，断网期间照常递增 → 天然时间锚。
  - 浏览器：沿用现有 `audio_bytes_total`，`offset_ms = bytes / (16000*2) * 1000`。
- **分段(Segment) 数据结构**（统一 live 与 offline）：

```jsonc
{
  "seg_id": "uuid",            // 稳定 id，便于替换
  "start_ms": 900000,         // 会议时间偏移(起)
  "end_ms": 903200,           // 会议时间偏移(止)
  "text": "……",
  "speaker_id": "spk_01|null",
  "speaker_final": false,      // 说话人是否已全局对齐
  "source": "live|offline",   // 来源通道
  "state": "provisional|filling|final",
  "revision": 3                // 复用 BLE caption 的 revision 思路，单调递增
}
```

- **排序与去重**：Timeline = 按 `start_ms` 排序的 Segment 列表。同一时间区间允许 live(provisional) 被 offline(final) **替换**（按 seg 覆盖的 [start,end] 区间匹配）。

---

## 5. 通道 B：可靠音频上传协议

### 5.1 传输选型：HTTP 分片 POST（推荐）

天然可重传、与 WS 解耦、易做断点续传。

```
POST /api/v1/sessions/{session_id}/audio
Headers: Authorization, X-Audio-Codec: pcm_s16le|ima_adpcm, X-Sample-Rate: 16000
Query:   seq=<chunk_seq>&start_ms=<offset>&final=<0|1>
Body:    原始音频分片(若干帧)
→ 200 {"acked_seq": N, "next_expected_seq": N+1}
```

- 客户端/录音卡维护"已 ack 的最大 seq"，未 ack 的重传。
- 后端把分片按 `start_ms` 写入 `audio_cache/{session_id}.pcm`（稀疏写或带索引），并记录已收到的区间 `[start_ms,end_ms]` 集合（用于检测缺口）。
- `final=1` 标记该会议音频上传完毕。

### 5.2 缺口(Gap)登记与触发离线转写

后端维护每个会议的**已覆盖区间**。当出现"一段连续区间补齐"时（如断网段 15:00–25:00 的分片到齐），登记一个**离线转写任务**：

```jsonc
OfflineJob { session_id, range:[start_ms,end_ms], reason:"gap|bulk|finalize", status:"queued|running|done|failed" }
```

- `gap`：断网补传段。
- `bulk`：录音卡一次性批量上传（range 可为整场）。
- `finalize`：会议结束，对完整音频做一次权威重转（可选，质量最高）。

---

## 6. 离线转写服务

### 6.1 部署

新增 compose 服务 `funasr-offline`（与现有 online 并存）：

```yaml
funasr-offline:
  profiles: ["offline-asr"]
  image: registry.cn-hangzhou.aliyuncs.com/funasr_repo/funasr:funasr-runtime-sdk-cpu-<ver>   # 阶段一: CPU
  # 阶段二改 GPU 镜像 + deploy.resources.reservations.devices(nvidia)
  volumes: [ ../server/data/audio_cache:/audio:ro, <models>:/workspace/models ]
  cpus: 3.0          # 批处理多给核（实时流那个仍 1.5）
  mem_limit: 6g
```

### 6.2 客户端

新增 `server/app/funasr_offline_client.py`：输入一段 PCM(或 [start,end] 区间) → 调离线服务（VAD+ASR+标点）→ 返回带 `start_ms/end_ms/text` 的分段列表（offset 需加上区间基准）。

### 6.3 算力预期（本机 Ryzen 1500X / GTX 1060 3GB）

| 模式 | 2h 音频耗时 | 备注 |
|---|---|---|
| CPU 离线 | 约 25~50 min | 阶段一，零驱动风险 |
| GPU 离线(1060) | 约 3~8 min | 阶段二，需装驱动+container-toolkit；3GB 显存须 FP16/小 batch |

10 分钟断网段：CPU ~2~5min / GPU ~20~40s。

---

## 7. 补洞与合并逻辑（后端）

离线任务完成后：

1. **替换/插入**：用 offline 分段（`source=offline, state=final`）覆盖 Timeline 中 `[start,end]` 区间内的 provisional 分段（按区间相交匹配，旧的标记删除/被盖）。
2. **持久化**：更新 `transcripts/{id}.json`（分段集），保持按 `start_ms` 有序。
3. **推送**：通过实时 WS 向当前连接 + observers 广播 `segments_patch` 事件（见 §11）。
4. **重算滚动纪要**：以合并后的完整 transcript 重跑一次 `summarize(rolling=True)` → 广播 `meeting_update`。
5. **说话人**：offline 段的 speaker 先标 `speaker_final=false`（临时），会议结束统一对齐（§9）。

---

## 8. 会议分段状态机

```
provisional ──(离线段覆盖)──▶ final
   ▲                            ▲
   │(live实时出)                 │(会议结束 finalize 重转/全局说话人对齐)
断网区间: (无live) ─登记gap─▶ filling ─(离线转完)─▶ final
```

- `provisional`：实时流产出，未经离线校验。
- `filling`：已知有音频、正在离线转写的区间（UI 显示"补传中…"占位）。
- `final`：离线权威结果（或会议结束定稿）。

会议级状态：`recording → (ended)uploading → transcribing → done`（复用现有 `summary_pending` UI 模式）。

---

## 9. 说话人对齐

- **实时/补洞阶段**：各自分配的 `spk_xx` 都是**局部、临时**的（`speaker_final=false`）。
- **会议结束**：用现有 `post_meeting_diarizer.py` 思路，对完整音频做一次**全局说话人聚类**，产出统一的说话人映射，回填所有分段（`speaker_final=true`）。
- UI 对 `speaker_final=false` 的说话人标签可加"（待定）"弱提示。

---

## 10. 前端时间轴渲染

- `CaptionStream` 改为**按 `start_ms` 排序渲染**，而非数组到达序。
- `filling` 区间渲染占位卡片：`⏳ 此段网络中断，补传转写中…`。
- 收到 `segments_patch`：按 `seg_id`/区间替换，React key 用 `seg_id` 保证原位更新不闪。
- 滚动纪要：收到 `meeting_update` 整体刷新（现状即如此）。

---

## 11. 接口契约（新增/变更）

### HTTP
- `POST /api/v1/sessions/{id}/audio?seq=&start_ms=&final=` → `{acked_seq,next_expected_seq}`（§5.1）
- `GET  /api/v1/sessions/{id}/coverage` → 已覆盖区间 + 待补缺口（调试/恢复用）

### WebSocket（实时流，新增消息类型）
- `→ client` `segments_patch`：`{patches:[{seg_id,start_ms,end_ms,text,speaker_id,speaker_final,state,revision}], removed:[seg_id...]}`
- `→ client` `gap_marker`：`{start_ms,end_ms,state:"filling"}`（断网区间登记，前端显示占位）
- 现有 `session_resumed` 扩展：带上 Timeline 当前快照（含各段 state）。

### 协议常量
在 `protocol.py` 的 `MessageType` 增加 `SEGMENTS_PATCH`、`GAP_MARKER`。

---

## 12. 数据模型与存储

- `transcripts/{id}.json`：`segments` 从"到达序数组"改为"按 start_ms 有序的 Segment 列表"（结构见 §4）。需写**迁移/兼容**：旧记录无 offset 时按索引退化排序。
- `audio_cache/{id}.pcm`：支持按 offset 稀疏写入（断网段后补）。需要一个 `.idx` 记录已写区间，或上传完成后顺序重整。
- 存储量：PCM 16k/16bit/mono ≈ 230MB/2h；录音卡 ADPCM(4:1) ≈ 57MB/2h。**需定清理策略**（如转写完成后降采样归档/仅留 ADPCM）。

---

## 13. 分阶段实施

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 地基** | Segment 加 `start_ms/seg_id/state`；Timeline 按时间排序；前端按 seg_id 渲染 | 现有实时录音不回归，字幕按时间序 |
| **P1 通道B(浏览器)** | HTTP 分片上传 + 后端拼 .pcm + coverage 缺口检测 | 浏览器断网后音频完整到后端（不依赖实时流） |
| **P2 离线服务(CPU)** | `funasr-offline` 服务 + offline client + OfflineJob 队列 | 一段音频能离线转出带时间戳分段 |
| **P3 补洞合并** | gap 触发 → 离线转 → `segments_patch` 原位插入 → 重算纪要 | 浏览器模拟断网，断网段字幕原位补齐 |
| **P4 录音卡接入** | 录音卡走通道B(ADPCM 分片+断点续传)；批量上传 | 录音卡断网10min重连，内容补齐 |
| **P5 GPU 提速** | 装 N 卡驱动 + container-toolkit + GPU 镜像 | 2h 音频转写从 ~30min 降到 ~5min |
| **P6 定稿对齐** | 会议结束 finalize 全局说话人 + 可选整场重转 | 最终转写说话人统一、无缺口 |

> 先用浏览器把 P0–P3 跑通验证（无需录音卡硬件），再接 P4 录音卡。

---

## 14. 预计改动文件清单

**后端**
- `server/app/protocol.py`：新增消息类型 + Segment 模型
- `server/app/storage.py`：segments 有序化 + 迁移兼容 + 音频区间索引
- `server/app/audio_buffer.py`：支持按 offset 写入
- `server/app/session_manager.py`：Timeline 维护（有序分段集 + patch）
- `server/app/ws_gateway.py`：广播 `segments_patch`/`gap_marker`；实时段标 provisional
- `server/app/audio_upload_api.py`（新）：HTTP 分片上传 + coverage
- `server/app/funasr_offline_client.py`（新）：离线转写客户端
- `server/app/offline_jobs.py`（新）：OfflineJob 队列 + 补洞合并 + 重算纪要
- `deploy/docker-compose.yml`：`funasr-offline` 服务
- `server/tests/`：新增 offline_jobs / 上传 / 合并 单测

**前端**
- `apps/web-client/src/CaptionStream.jsx`：按 start_ms 排序 + 占位卡 + seg_id key
- `apps/web-client/src/MeetingView.jsx`：处理 `segments_patch`/`gap_marker`；接入通道B上传器；移除/降级限速补传
- `apps/web-client/src/audioUploader.js`（新）：HTTP 分片 + 断点续传 + ack

**录音卡（P4）**
- `firmware/nrf52840`：断网本地缓存 + 重连按 seq 断点续传（复用现有 chunk 恢复协议）
- 协议文档 + 测试向量同步更新（见 `protocol/README.md` 的"同改"约定）

---

## 15. 风险与权衡

- **复杂度上升**：append-only → 时间序插入 + 状态机，前后端都更复杂。用 `seg_id` + 区间替换控制。
- **补洞非瞬时**：洞填上要等离线转完（GPU 几十秒 / CPU 几分钟），UI 必须有"补传中"占位管理预期。
- **存储**：长会议音频占空间，需清理策略。
- **DeepSeek 成本**：每次合并重算滚动纪要多花 token，可接受；可加节流（区间变化才重算）。
- **GPU 显存 3GB 紧**：FP16 + 小 batch，必要时标点模型放 CPU；服务器可关 GNOME 释放显存。
- **双写带宽**：实时 + 可靠上传同时传，PCM 约 512kbps（ADPCM 更低），普通网络无压力。

---

## 16. 决策记录

### 2026-06-30 初版拍板
1. **通道 B 传输 = HTTP 分片 POST** ✅（独立于实时 WS，天然断点续传）
2. 音频留存格式：暂留 PCM，ADPCM 归档作为后续优化（未定，不阻塞）
3. ~~`finalize` 整场重转 = 默认每场都做~~（已被 2026-07-04 触发模型重构取代，见下）
4. 滚动纪要重算节流：先"每次补洞都重算"，后续按需加节流（不阻塞）
5. GPU 阶段（P5）：接受装驱动需重启、显示输出共用 1060 ✅ 已完成

**起步范围 = P0–P3 全骨架**，用浏览器模拟断网端到端验证，不依赖录音卡硬件。✅ 已完成并验证。

### 2026-07-04 触发模型重构（架构评审决策 1，`lifecycle.py`）

整场 finalize 重转被废弃，原因：① 与 meeting_end 落盘存在竞态（短会议补洞成果被覆盖）；
② fill_gaps 丢弃与实时段重叠的绝大多数转写结果，GPU 算力浪费 ~90%；③ 整场按字数摊
时间戳在长会议（含静默）严重漂移；④ 最终纪要 DeepSeek 算两次。

**新模型（MeetingLifecycle 协调）：**
- **录制中**：重连 `timeline_advance` 时 `register_gap()` 注册断网洞；通道 B 每片上传后
  `on_upload_progress()` 检查洞的音频是否补齐（`CoverageTracker.contains`），补齐立即
  **只转该洞**（`reason="gap"`，`apply_offline` 原位替换，base_offset 精确无漂移）→
  `segments_patch` 推送替换前端占位 → 滚动纪要刷新（含补回内容）。
- **会议结束**：`meeting_end` 先短暂等音频尾片（默认 10s，`MEETING_END_AUDIO_WAIT_SECONDS`）
  → **先落盘 transcript（以 timeline 为唯一来源，含已补的洞）再入队任何任务**（消除竞态）→
  `can_finish_inline()`（音频完整 && 无未处理洞 && 队列空闲）？
  - 是（常见路径）：当场出唯一一次最终纪要，完整 MEETING_RESULT，UX 与旧版一致；
  - 否：会话挂入 `finalizing`，回占位 MEETING_RESULT（`pending=true`，summary_pending
    由历史页轮询接管）；音频完整后仅转残余洞，队列尾部 `summarize` 哨兵任务
    （单 worker FIFO 保证在补洞之后）出唯一一次最终纪要。
- **兜底**：final 分片永远不来（客户端死亡）→ `orphan_timeout`（600s）后强制用现有内容
  出纪要，历史页不会卡在"生成中"。
