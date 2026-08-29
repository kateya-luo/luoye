# ClearMeeting v0.20.1 离线流水线启动恢复补丁

本补丁针对 v0.20.0 首次部署日志中的两种情况：CPU FunASR 加载模型期间连接拒绝时，把持久任务重试窗口扩展到足够长；历史设备会话缺失会议元数据时不再把离线任务误记为成功。录音文件不会被删除。

从 v0.20.0 升级时重复执行 `DEPLOYMENT-V0.20.0.md` 的上传、校验、备份和覆盖步骤，将包名及目录版本改为 `v0.20.1`，然后写入：

```bash
cd /home/luozhou/clearmeeting/deploy
set_env() { key="$1"; value="$2"; grep -q "^${key}=" .env && sed -i "s|^${key}=.*|${key}=${value}|" .env || printf '%s=%s\n' "$key" "$value" >> .env; }
set_env SERVER_RELEASE clearmeeting-server-v0.20.1
set_env OFFLINE_ASR_MAX_RETRIES 30
set_env OFFLINE_ASR_RETRY_BASE_SECONDS 5

docker compose --profile real-asr --profile offline-asr-cpu build server
docker compose --profile real-asr --profile offline-asr-cpu up -d \
  funasr funasr-offline-cpu speaker server nginx
EXPECTED_SERVER_VERSION=0.20.1 bash ./verify_api_v2.sh http://127.0.0.1
```

检查离线服务与持久任务：

```bash
docker compose --profile real-asr --profile offline-asr-cpu ps
docker compose logs --tail=150 funasr-offline-cpu
docker compose logs --since=15m server | grep -E 'offline_(job|persistent)|device_(asr|complete)' | tail -n 150
```
