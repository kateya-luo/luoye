# ClearMeeting v0.17.0 部署说明

本版本把实时会议中的“滚动纪要”升级为服务器权威的时间轴章节，并将录音卡 MARK 按真实录音时间归入章节。无需重新烧录落叶固件 v0.9.2。

## 部署前准备

- 服务器项目目录：`/srv/clearmeeting/clearmeeting`
- 发布包：`clearmeeting-server-v0.17.0-timeline-r1.zip`
- 保留现有 `.env` 和 `server/data`，不要删除数据库。

## 升级步骤

```bash
cd /srv/clearmeeting
sha256sum -c clearmeeting-server-v0.17.0-timeline-r1.zip.sha256
rm -rf /srv/clearmeeting/clearmeeting-v0.17.0
mkdir -p /srv/clearmeeting/clearmeeting-v0.17.0
unzip -q clearmeeting-server-v0.17.0-timeline-r1.zip -d /srv/clearmeeting/clearmeeting-v0.17.0
cd /srv/clearmeeting/clearmeeting-v0.17.0
sha256sum -c SHA256SUMS.txt
```

备份数据库并覆盖程序文件：

```bash
cd /srv/clearmeeting/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-$(date +%Y%m%d-%H%M%S).db"
rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.17.0/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.17.0/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.17.0/apps/card-sim/ apps/card-sim/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.17.0/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.17.0/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.17.0/package.json .
cp /srv/clearmeeting/clearmeeting-v0.17.0/package-lock.json .
```

重新构建并启动：

```bash
cd /srv/clearmeeting/clearmeeting/deploy
docker compose build --no-cache api web
docker compose up -d
docker compose ps
docker compose logs --tail=100 api
```

验证版本：

```bash
curl -s http://127.0.0.1:34567/api/v2/build-info
```

返回内容应包含 `"server_version":"0.17.0"`。随后用录音卡录音、按一次 MARK，网页“滚动纪要”应出现真实时间章节和可点击 MARK。
