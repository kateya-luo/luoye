# ClearMeeting v0.18.0 部署说明

本版本配套落叶 v0.9.3，增加录音卡时间戳章节显示，并修复“设备开关已开启、CAM++ 实际未工作但界面仍显示正常”的问题。

## 1. 上传和校验发布包

在 Windows PowerShell 中执行（把服务器地址按实际情况替换）：

```powershell
cd D:\OPENOP\files-mentioned-by-the-user-chatgpt\clearmeeting\releases
scp .\clearmeeting-server-v0.18.0-device-timeline-speaker-r1.zip deploy@192.0.2.10:/srv/clearmeeting/
scp .\clearmeeting-server-v0.18.0-device-timeline-speaker-r1.zip.sha256 deploy@192.0.2.10:/srv/clearmeeting/
ssh deploy@192.0.2.10
```

登录服务器后执行：

```bash
cd /srv/clearmeeting
sed -i 's/\r$//' clearmeeting-server-v0.18.0-device-timeline-speaker-r1.zip.sha256
sha256sum -c clearmeeting-server-v0.18.0-device-timeline-speaker-r1.zip.sha256
rm -rf /srv/clearmeeting/clearmeeting-v0.18.0
mkdir -p /srv/clearmeeting/clearmeeting-v0.18.0
unzip -q clearmeeting-server-v0.18.0-device-timeline-speaker-r1.zip -d /srv/clearmeeting/clearmeeting-v0.18.0
cd /srv/clearmeeting/clearmeeting-v0.18.0
sha256sum -c SHA256SUMS.txt
```

## 2. 备份数据库并覆盖程序

```bash
cd /srv/clearmeeting/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-$(date +%Y%m%d-%H%M%S).db"

rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.18.0/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.18.0/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.18.0/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /srv/clearmeeting/clearmeeting-v0.18.0/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.18.0/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.18.0/package.json .
cp /srv/clearmeeting/clearmeeting-v0.18.0/package-lock.json .
```

## 3. 确认多人语音配置

```bash
cd /srv/clearmeeting/clearmeeting/deploy
grep -q '^SPEAKER_MODE=' .env && sed -i 's/^SPEAKER_MODE=.*/SPEAKER_MODE=remote/' .env || echo 'SPEAKER_MODE=remote' >> .env
grep -q '^SPEAKER_EMBEDDING_URL=' .env && sed -i 's|^SPEAKER_EMBEDDING_URL=.*|SPEAKER_EMBEDDING_URL=http://speaker:10100/embed|' .env || echo 'SPEAKER_EMBEDDING_URL=http://speaker:10100/embed' >> .env
grep -q '^SERVER_RELEASE=' .env && sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v0.18.0/' .env || echo 'SERVER_RELEASE=clearmeeting-server-v0.18.0' >> .env
```

不要删除原有 `.env`，上面只修改三项，不会覆盖账号密码或 API Key。

## 4. 重建并启动全部依赖

```bash
cd /srv/clearmeeting/clearmeeting/deploy
docker compose --profile real-asr build --no-cache speaker server nginx
docker compose --profile real-asr up -d funasr speaker server nginx
docker compose ps
```

首次启动 speaker 可能需要下载 CAM++ 模型。等待它变成 healthy：

```bash
until docker compose ps speaker | grep -q '(healthy)'; do
  echo '等待 CAM++ 声纹模型就绪...'
  sleep 10
done
docker compose logs --tail=80 speaker
docker compose logs --tail=80 server
```

## 5. 验证版本和真实声纹依赖

```bash
curl -fsS http://127.0.0.1/api/v2/build-info
echo
curl -fsS http://127.0.0.1/health/ready
echo
```

必须同时看到：

- `server_version` 为 `0.18.0`
- `minimum_firmware` 为 `0.9.3`
- `speaker.ready` 为 `true`
- `speaker.mode` 为 `remote`

如果 `speaker.ready` 不是 `true`，先不要测试录音卡，执行：

```bash
docker compose logs --tail=200 speaker
docker compose logs --tail=200 server
```

## 6. 真机验收

1. 烧录落叶 v0.9.3。
2. 网页设备管理确认“多人语音识别”显示“云端声纹服务可用”。
3. 开始一场新会议；旧会议不会因开关改变而重新分配说话人。
4. 两个人轮流各说至少两句，每句尽量超过 1.2 秒。
5. 串口应出现 `LY|SPEAKER|... enabled=1 labeled=... speakers=...`，并且数字逐步增加。
6. 墨水屏应显示 `[MM:SS] 章节标题` 及最多两条章节内容。
7. 网页实时字幕和会议历史应出现“说话人 1 / 说话人 2”。
