# ClearMeeting v0.19.2 部署教程

本版本只升级服务器和网页，不需要重新烧录落叶固件。网页端继续保留章节全部要点，设备接口只向录音卡返回当前章节最新两条要点。升级保留数据库、录音、账号和 `deploy/.env`。

## 1. Windows 上传发布包

在存放压缩包的目录打开普通 Windows PowerShell，逐行执行：

```powershell
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.2-device-latest-points-r1.zip" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.2-device-latest-points-r1.zip.sha256" "deploy@192.0.2.10:/srv/clearmeeting/"
& "$env:WINDIR\System32\OpenSSH\ssh.exe" "deploy@192.0.2.10"
```

以上三条是 Windows 命令，不要粘贴到 Ubuntu 终端。

## 2. Ubuntu 校验并解压

```bash
cd /srv/clearmeeting
sha256sum -c clearmeeting-server-v0.19.2-device-latest-points-r1.zip.sha256
rm -rf /srv/clearmeeting/clearmeeting-v0.19.2
mkdir -p /srv/clearmeeting/clearmeeting-v0.19.2
unzip -q clearmeeting-server-v0.19.2-device-latest-points-r1.zip -d /srv/clearmeeting/clearmeeting-v0.19.2
cd /srv/clearmeeting/clearmeeting-v0.19.2
sha256sum -c SHA256SUMS.txt
```

外层和内部校验都应显示 `OK`。

## 3. 备份数据库

```bash
cd /srv/clearmeeting/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-before-v0.19.2-$(date +%Y%m%d-%H%M%S).db"
```

## 4. 覆盖程序并保留数据和配置

```bash
cd /srv/clearmeeting/clearmeeting
rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.2/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.2/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.2/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.2/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.2/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.19.2/package.json .
cp /srv/clearmeeting/clearmeeting-v0.19.2/package-lock.json .
test -f server/data/clearmeeting.db && echo '数据库仍在：OK'
test -f deploy/.env && echo '.env 仍在：OK'
```

## 5. 更新版本并重建

```bash
cd /srv/clearmeeting/clearmeeting/deploy
grep -q '^SERVER_RELEASE=' .env \
  && sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v0.19.2/' .env \
  || echo 'SERVER_RELEASE=clearmeeting-server-v0.19.2' >> .env
docker compose --profile real-asr build --no-cache server nginx
docker compose --profile real-asr up -d funasr speaker server nginx
docker compose ps
```

## 6. 验证

```bash
cd /srv/clearmeeting/clearmeeting/deploy
curl -fsS http://127.0.0.1/api/v2/build-info
echo
curl -fsS http://127.0.0.1/health/ready
echo
EXPECTED_SERVER_VERSION=0.19.2 bash ./verify_api_v2.sh http://127.0.0.1
```

确认 `server_version` 为 `0.19.2`。录制一场产生至少四条章节要点的新会议：网页端应显示全部要点，落叶屏幕应显示同一章节最后两条。
