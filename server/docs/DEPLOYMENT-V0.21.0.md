# ClearMeeting Server V0.21.0 部署说明

本版把会议过程改为纯实时转写；会议完整转写与离线多人识别完成后，由用户在历史页选择模板，一次性调用 DeepSeek 生成正式纪要。

## 部署

压缩包解压后是完整的项目根目录。生产环境使用稳定路径
`/home/luozhou/clearmeeting`，所以应将新版代码同步进该目录，同时排除
`server/data` 和 `deploy/.env`。

```bash
cd /home/luozhou
unzip -q clearmeeting-server-v0.21.0-upload-progress-r9.zip -d clearmeeting-v0.21.0-release

# 1. 稳定备份 SQLite（包含 WAL 中尚未 checkpoint 的数据）
mkdir -p /home/luozhou/clearmeeting-backups/v0.21.0-predeploy
cd /home/luozhou/clearmeeting/deploy
docker compose exec -T server python -c "import sqlite3; s=sqlite3.connect('/app/data/clearmeeting.db'); d=sqlite3.connect('/app/data/clearmeeting.db.pre-v0.21.0'); s.backup(d); d.close(); s.close()"
cp -p .env /home/luozhou/clearmeeting-backups/v0.21.0-predeploy/deploy.env

# 2. 更新代码，不覆盖会议数据和密钥
rsync -a --delete \
  --exclude 'server/data/' \
  --exclude 'deploy/.env' \
  /home/luozhou/clearmeeting-v0.21.0-release/ /home/luozhou/clearmeeting/

cd /home/luozhou/clearmeeting/deploy
sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v0.21.0/' .env
grep -q '^DEEPSEEK_MINUTES_MODEL=' .env || echo 'DEEPSEEK_MINUTES_MODEL=deepseek-v4-flash' >> .env

# 3. 只重建本版改动的 server 和 nginx，不重建 ASR 模型
docker compose --profile real-asr --profile offline-asr-cpu build server nginx
docker compose --profile real-asr --profile offline-asr-cpu up -d server nginx

EXPECTED_SERVER_VERSION=0.21.0 bash ./verify_api_v2.sh http://127.0.0.1
```

R5 修复模板纪要已经生成后，公网瞬时 `502/503/504` 导致页面误报失败的问题：
页面会先使用纪要任务返回的结果并关闭模板选择器，再刷新会议详情；幂等 GET
请求会短暂重试，POST 生成请求不会自动重试，避免重复调用模型。

R6 将导出文件名改为会议名称，移除 Word 纪要章节旁的时间标签，并按会议
记住最近一次导出格式和内容选项。会议详情重新打开时继续显示数据库中当前
生效的模板纪要，不重新回到初始模板状态；原始字幕时间轴仍按用户勾选保留。

R7 修复重新选择一个已经生成过的模板时，服务器直接返回幂等 `ready` 任务而
网页按钮没有反应的问题。新任务轮询完成与已有任务立即命中现在统一进入同一
显示路径：关闭模板选择器、立即展示保存结果，并容错刷新会议详情。

R8 为离线上传和会后整理增加可见的后台处理进度。网页结束页、历史列表和会议
详情会自动显示“音频上传、排队、离线转写、整场多人识别、写回完成”五类真实
阶段，并显示可靠的上传比例、已完成任务数、前方排队任务数和基于本机历史处理
速度计算的预计时间区间。FunASR 单个请求内部没有进度回调，因此单片处理中不会
伪造持续增长的百分比；页面可关闭，服务器仍会继续处理。若会议删除时仍有后台
任务，任务会同步取消，晚到的失败回调也不能把它重新放回队列；超过 5 分钟处于
`finalizing` 但队列中没有任务的异常会议会明确显示“后台处理已停滞”。

R9 修正补传阶段进度含义：进度条直接显示服务器已经持久化并确认的音频覆盖率，
不再把上传率乘以 35% 冒充总流程百分比；同时显示尚缺音频时长，并将任务数明确
标注为“已接收部分转写”，避免把局部 22/22 误解成整场录音已经到齐。覆盖率按已
确认字节区间的并集计算，稀疏文件大小和重复补传都不会虚增进度。

数据库迁移只增加字段和新表，不删除旧会议、旧纪要或音频。首次启动
V0.21.0 前不要删除上述备份。

录音卡与网页在线会议的结束语义已统一：可靠音频完整后，都先执行整场
canonical ASR 与最终多人识别，只有成功写回规范转写后才进入 `transcript_ready`。

## 验证

```bash
curl -s http://127.0.0.1/api/v2/build-info
curl -s http://127.0.0.1/health/ready
docker compose ps
docker compose logs --since=10m server | grep -E 'minutes_job|transcript_ready|ERROR'
```

登录网页后也可以用轻量接口核对单场会议的后台状态：

```text
GET /api/v1/meetings/{session_id}/processing
```

`build-info` 应包含：`transcript_only_live_v1`、`template_minutes_v1`、`editable_meeting_speakers_v1`、`meeting_memory_v1`、`on_demand_minutes_v1`，并且不再包含 `device_rolling_minutes`。

## 回退

将部署前代码备份同步回 `/home/luozhou/clearmeeting`，保留 `server/data`
与 `.env`，然后重建 `server nginx`。V0.20.3 会忽略本版新增的表和字段；
若需完整数据库回退，必须先停服务，再恢复 `clearmeeting.db.pre-v0.21.0`。
