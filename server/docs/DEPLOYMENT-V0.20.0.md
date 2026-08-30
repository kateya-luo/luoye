# ClearMeeting v0.20.0 长录音离线流水线部署教程

本版与固件 V1.2.0 对齐：固件继续按公网 10 MiB 可靠范围上传；服务器每当已校验字节覆盖一个完整 5 分钟窗口，就提前建立持久化 ASR 任务。最后不足 5 分钟的尾片只在 `/complete` 后封口。任务默认 4 Worker，重启可恢复，全部切片完成前不会生成最终纪要。

两小时 16 kHz/16-bit/mono PCM 约 219.7 MiB，对应 22 个上传范围、24 个 5 分钟 ASR 切片。上传颗粒度与转写颗粒度独立，服务器不要求固件把录音裁成 5 分钟文件。

## 1. Windows 上传

```powershell
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.20.0-offline-pipeline-r1.zip" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.20.0-offline-pipeline-r1.zip.sha256" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\ssh.exe" "deploy@192.0.2.10"
```

## 2. Ubuntu 校验、解压和备份

```bash
cd /srv/clearmeeting
sha256sum -c clearmeeting-server-v0.20.0-offline-pipeline-r1.zip.sha256
mkdir -p /srv/clearmeeting/clearmeeting-v0.20.0
unzip -q clearmeeting-server-v0.20.0-offline-pipeline-r1.zip -d /srv/clearmeeting/clearmeeting-v0.20.0
cd /srv/clearmeeting/clearmeeting-v0.20.0
sha256sum -c SHA256SUMS.txt

cd /srv/clearmeeting/clearmeeting
mkdir -p backups
stamp="$(date +%Y%m%d-%H%M%S)"
cp -a server/data/clearmeeting.db "backups/clearmeeting-before-v0.20.0-${stamp}.db"
cp -a deploy/.env "backups/env-before-v0.20.0-${stamp}"
```

## 3. 覆盖代码，保留数据库和密钥

```bash
cd /srv/clearmeeting/clearmeeting
rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.20.0/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.20.0/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.20.0/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /srv/clearmeeting/clearmeeting-v0.20.0/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.20.0/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.20.0/package.json .
cp /srv/clearmeeting/clearmeeting-v0.20.0/package-lock.json .
test -f server/data/clearmeeting.db && echo '数据库保留：OK'
test -f deploy/.env && echo '.env 保留：OK'
```

## 4. 写入流水线参数

当前主机为 8 核、16 GiB、无显卡生产方案，推荐用独立 CPU 离线 FunASR，避免两小时录音抢占实时字幕服务。

```bash
cd /srv/clearmeeting/clearmeeting/deploy
set_env() { key="$1"; value="$2"; grep -q "^${key}=" .env && sed -i "s|^${key}=.*|${key}=${value}|" .env || printf '%s=%s\n' "$key" "$value" >> .env; }
set_env SERVER_RELEASE clearmeeting-server-v0.20.0
set_env DEVICE_OFFLINE_ASR_WINDOW_MS 300000
set_env OFFLINE_ASR_WORKERS 4
set_env OFFLINE_ASR_LEASE_SECONDS 7200
set_env FUNASR_OFFLINE_WS_URL ws://funasr-offline-cpu:10095
set_env OFFLINE_ASR_MODE 2pass
```

原有 `FunASR=2.25 CPU/6 GiB`、`Speaker=1.5 CPU/3 GiB` 保持不变；新增离线 CPU 服务上限为 `2.5 CPU/5 GiB`。这些都是上限，不是启动即占满。

## 5. 重建并启动

```bash
awk '/MemTotal/ {print "Linux 可见内存：", $2/1024/1024, "GiB"; if ($2 < 15000000) exit 1}' /proc/meminfo \
  || { echo 'Linux 未识别完整 16 GiB，停止部署'; exit 1; }

cd /srv/clearmeeting/clearmeeting/deploy
docker compose --profile real-asr --profile offline-asr-cpu config >/dev/null
docker compose --profile real-asr --profile offline-asr-cpu build server nginx
docker compose --profile real-asr --profile offline-asr-cpu pull funasr-offline-cpu
docker compose --profile real-asr --profile offline-asr-cpu up -d \
  funasr funasr-offline-cpu speaker server nginx
docker compose ps
```

首次启动会自动给现有 SQLite 增加 `offline_asr_jobs` 表，不删除会议和账号数据。

## 6. 验证

```bash
curl -fsS http://127.0.0.1/health/ready; echo
curl -fsS http://127.0.0.1/api/v2/build-info; echo
EXPECTED_SERVER_VERSION=0.20.0 bash ./verify_api_v2.sh http://127.0.0.1

docker inspect ai-recorder-system-funasr-offline-cpu-1 \
  --format 'Offline FunASR CPU={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}}'
docker stats --no-stream \
  --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}'
docker compose logs --since=10m server | grep -E 'offline_(job|persistent)|range_schedule' | tail -n 100
```

期望离线容器限制为 `2500000000 / 5368709120 / 7516192768`，`build-info` 包含 `offline_asr_pipeline_v1`。

查看持久任务，不依赖宿主机安装 sqlite3：

```bash
docker compose exec -T server python - <<'PY'
import sqlite3
db = sqlite3.connect('/app/data/clearmeeting.db')
for row in db.execute("SELECT state,reason,COUNT(*) FROM offline_asr_jobs GROUP BY state,reason ORDER BY state,reason"):
    print(row)
PY
```

## 7. 实测顺序

先做 15 分钟录音，确认上传尚未结束时日志已出现 5 分钟切片；再做至少 2 小时录音，确认任务总数为 24（若时长不是 5 分钟整倍数则多一个尾片）、中途重启 `server` 后任务继续、最终纪要只生成一次。两台设备并发时重点观察实时字幕是否流畅，以及 `funasr-offline-cpu` 是否持续工作而 `funasr` 没被离线任务拖满。

## 8. 回滚

若需要回滚，先停止新增的离线实例，再按 v0.19.3 包覆盖代码；新表对旧版无影响，通常不必回滚数据库。

```bash
cd /srv/clearmeeting/clearmeeting/deploy
docker compose --profile offline-asr-cpu stop funasr-offline-cpu
sed -i '/^FUNASR_OFFLINE_WS_URL=/d;/^OFFLINE_ASR_MODE=/d;/^OFFLINE_ASR_WORKERS=/d;/^OFFLINE_ASR_LEASE_SECONDS=/d' .env
# 然后按 DEPLOYMENT-V0.19.3.md 第 3–6 步覆盖并启动 v0.19.3。
```
