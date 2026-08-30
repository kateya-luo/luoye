# ClearMeeting Server V2.0.0 部署说明

V2.0.0 是已稳定运行的 V0.21.0 R9 的正式版本升级。除版本标识外，不改变
上传、实时与离线转写、多人声纹、模板纪要、会议记忆或导出行为；设备 API
契约继续为 `luoye-device-api/2`，数据库仅沿用既有兼容迁移。

## 部署

```bash
cd /srv/clearmeeting
unzip -q clearmeeting-server-v2.0.0-stable-r1.zip -d clearmeeting-v2.0.0-stable-r1

# 1. 备份数据库与生产配置
mkdir -p /srv/clearmeeting/clearmeeting-backups/v2.0.0-predeploy
cd /srv/clearmeeting/clearmeeting/deploy
docker compose exec -T server python -c "import sqlite3; s=sqlite3.connect('/app/data/clearmeeting.db'); d=sqlite3.connect('/app/data/clearmeeting.db.pre-v2.0.0'); s.backup(d); d.close(); s.close()"
cp -p .env /srv/clearmeeting/clearmeeting-backups/v2.0.0-predeploy/deploy.env

# 2. 更新程序，保留会议数据、密钥和生产配置
rsync -a --delete \
  --exclude 'server/data/' \
  --exclude 'deploy/.env' \
  /srv/clearmeeting/clearmeeting-v2.0.0-stable-r1/ /srv/clearmeeting/clearmeeting/

cd /srv/clearmeeting/clearmeeting/deploy
if grep -q '^SERVER_RELEASE=' .env; then
  sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v2.0.0/' .env
else
  echo 'SERVER_RELEASE=clearmeeting-server-v2.0.0' >> .env
fi

# 3. 只重建业务服务与网页代理，不重新构建 ASR 模型
docker compose --profile real-asr --profile offline-asr-cpu build server nginx
docker compose --profile real-asr --profile offline-asr-cpu up -d server nginx

EXPECTED_SERVER_VERSION=2.0.0 bash ./verify_api_v2.sh http://127.0.0.1
```

验收时 `/api/v2/build-info` 必须返回：

```json
{
  "server_version": "2.0.0",
  "server_release": "clearmeeting-server-v2.0.0",
  "api_contract": "luoye-device-api/2"
}
```

## 回退

将部署前代码备份同步回 `/srv/clearmeeting/clearmeeting`，保留 `server/data` 与
`deploy/.env` 后重建 `server nginx`。若程序回退仍不足，停服务后再恢复
`clearmeeting.db.pre-v2.0.0`；数据库恢复前应先额外保存当前数据库。
