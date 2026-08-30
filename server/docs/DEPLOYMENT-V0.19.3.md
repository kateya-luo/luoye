# ClearMeeting v0.19.3 声纹稳定性版部署教程

本版完成两阶段声纹优化：声纹音频独立限制为 2–12 秒并过滤低能量片段；新人物需连续两次一致证据才建立，聚类人数动态增长且不设人数上限，会议结束后再进行全局聚类修正。同时将 FunASR 调整为 2.25 CPU/6 GiB，Speaker 调整为 1.5 CPU/3 GiB。

> 重要：资源上限按 16 GiB 主机设计。若 `grep MemTotal /proc/meminfo` 仍只有约 8 GiB，请先解决 BIOS/内存映射问题，不要启动本版的新资源配置，否则高峰期仍可能触发系统 OOM。

## 1. Windows 上传发布包

```powershell
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.3-speaker-stability-r1.zip" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.3-speaker-stability-r1.zip.sha256" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\ssh.exe" "deploy@192.0.2.10"
```

## 2. Ubuntu 校验、解压并备份

```bash
cd /srv/clearmeeting
sha256sum -c clearmeeting-server-v0.19.3-speaker-stability-r1.zip.sha256
mkdir -p /srv/clearmeeting/clearmeeting-v0.19.3
unzip -q clearmeeting-server-v0.19.3-speaker-stability-r1.zip -d /srv/clearmeeting/clearmeeting-v0.19.3
cd /srv/clearmeeting/clearmeeting-v0.19.3
sha256sum -c SHA256SUMS.txt

cd /srv/clearmeeting/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-before-v0.19.3-$(date +%Y%m%d-%H%M%S).db"
```

## 3. 覆盖程序并保留数据和配置

```bash
cd /srv/clearmeeting/clearmeeting
rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.3/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.3/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.3/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.3/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.3/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.19.3/package.json .
cp /srv/clearmeeting/clearmeeting-v0.19.3/package-lock.json .
test -f server/data/clearmeeting.db && echo '数据库仍在：OK'
test -f deploy/.env && echo '.env 仍在：OK'
```

## 4. 写入本版参数

```bash
cd /srv/clearmeeting/clearmeeting/deploy
set_env() { key="$1"; value="$2"; grep -q "^${key}=" .env && sed -i "s|^${key}=.*|${key}=${value}|" .env || printf '%s=%s\n' "$key" "$value" >> .env; }
set_env SERVER_RELEASE clearmeeting-server-v0.19.3
set_env SPEAKER_SIMILARITY_THRESHOLD 0.68
set_env SPEAKER_CANDIDATE_SIMILARITY_THRESHOLD 0.72
set_env SPEAKER_CANDIDATE_CONFIRMATIONS 2
set_env SPEAKER_CANDIDATE_TTL_SEGMENTS 12
set_env SPEAKER_CLUSTER_MERGE_THRESHOLD 0.78
set_env SPEAKER_CLUSTER_MERGE_INTERVAL 20
set_env SPEAKER_MIN_SEGMENT_SECONDS 2.0
set_env SPEAKER_MAX_SEGMENT_SECONDS 12
set_env SPEAKER_MIN_RMS 80
set_env SPEAKER_REQUEST_TIMEOUT_SECONDS 20
set_env POST_MEETING_SPEAKER_THRESHOLD 0.68
sed -i '/^SPEAKER_MAX_COUNT=/d' .env
```

## 5. 内存前置检查并重建

```bash
awk '/MemTotal/ {print "Linux 可见内存：", $2/1024/1024, "GiB"; if ($2 < 15000000) exit 1}' /proc/meminfo \
  || { echo 'Linux 尚未识别完整 16 GiB，停止部署'; exit 1; }

cd /srv/clearmeeting/clearmeeting/deploy
docker compose --profile real-asr config >/dev/null
docker compose --profile real-asr build --no-cache speaker server nginx
docker compose --profile real-asr up -d funasr speaker server nginx
docker compose ps
```

## 6. 验证

```bash
curl -fsS http://127.0.0.1/api/v2/build-info; echo
curl -fsS http://127.0.0.1/health/ready; echo
EXPECTED_SERVER_VERSION=0.19.3 bash ./verify_api_v2.sh http://127.0.0.1
docker inspect ai-recorder-system-funasr-1 --format 'FunASR CPU={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'
docker inspect ai-recorder-system-speaker-1 --format 'Speaker CPU={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'
```

期望 FunASR 为 `2250000000 / 6442450944`，Speaker 为 `1500000000 / 3221225472`。随后用两台录音卡进行 10–15 分钟、两人交替说话测试；重点观察人物数是否稳定在真实人数附近，以及声纹请求日志中是否还出现超过 12 秒的请求。
