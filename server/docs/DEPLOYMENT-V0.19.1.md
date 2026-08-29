# ClearMeeting v0.19.1 热修复部署教程

本版本只升级服务器和网页，不需要重新烧录落叶固件。修复待办页无法滚动、多人语音设置界面粗糙，以及时间轴章节时间戳未绑定真实字幕段的问题。升级保留数据库、录音、账号和 `deploy/.env`。

## 1. Windows 上传发布包

打开普通 Windows PowerShell。若当前压缩包放在 `D:\创业项目\录音卡\fuwuqi`，逐行执行：

```powershell
cd "D:\创业项目\录音卡\fuwuqi"

& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.1-ui-timeline-hotfix-r1.zip" "luozhou@192.168.31.183:/home/luozhou/"
& "$env:WINDIR\System32\OpenSSH\scp.exe" ".\clearmeeting-server-v0.19.1-ui-timeline-hotfix-r1.zip.sha256" "luozhou@192.168.31.183:/home/luozhou/"

& "$env:WINDIR\System32\OpenSSH\ssh.exe" "luozhou@192.168.31.183"
```

注意：前三条是 Windows 命令，不要粘贴到 Ubuntu 终端。

## 2. Ubuntu 校验并解压

看到 `luozhou@...:~$` 后逐行执行：

```bash
cd /home/luozhou
ls -lh clearmeeting-server-v0.19.1-ui-timeline-hotfix-r1.zip*
sha256sum -c clearmeeting-server-v0.19.1-ui-timeline-hotfix-r1.zip.sha256

rm -rf /home/luozhou/clearmeeting-v0.19.1
mkdir -p /home/luozhou/clearmeeting-v0.19.1
unzip -q clearmeeting-server-v0.19.1-ui-timeline-hotfix-r1.zip -d /home/luozhou/clearmeeting-v0.19.1
cd /home/luozhou/clearmeeting-v0.19.1
sha256sum -c SHA256SUMS.txt
```

外层和内部校验都应显示 `OK`。v0.19.1 发布包已改为 Linux 兼容的 LF 校验文件和正斜杠 ZIP 路径，不需要再执行 `sed -i 's/\r$//'`。

## 3. 备份数据库

```bash
cd /home/luozhou/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-before-v0.19.1-$(date +%Y%m%d-%H%M%S).db"
ls -lh backups | tail
```

## 4. 覆盖程序，保留数据与配置

```bash
cd /home/luozhou/clearmeeting

rsync -a --delete --exclude 'data/' --exclude '.env' /home/luozhou/clearmeeting-v0.19.1/server/ server/
rsync -a --delete /home/luozhou/clearmeeting-v0.19.1/apps/web-client/ apps/web-client/
rsync -a --delete /home/luozhou/clearmeeting-v0.19.1/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /home/luozhou/clearmeeting-v0.19.1/deploy/ deploy/
rsync -a --delete /home/luozhou/clearmeeting-v0.19.1/docs/ docs/
cp /home/luozhou/clearmeeting-v0.19.1/package.json .
cp /home/luozhou/clearmeeting-v0.19.1/package-lock.json .

test -f server/data/clearmeeting.db && echo '数据库仍在：OK'
test -f deploy/.env && echo '.env 仍在：OK'
```

## 5. 更新版本标识并重建

```bash
cd /home/luozhou/clearmeeting/deploy

grep -q '^SERVER_RELEASE=' .env \
  && sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v0.19.1/' .env \
  || echo 'SERVER_RELEASE=clearmeeting-server-v0.19.1' >> .env

docker compose --profile real-asr build --no-cache server nginx
docker compose --profile real-asr up -d funasr speaker server nginx
docker compose ps
```

## 6. 验证

```bash
cd /home/luozhou/clearmeeting/deploy
curl -fsS http://127.0.0.1/api/v2/build-info
echo
curl -fsS http://127.0.0.1/health/ready
echo
EXPECTED_SERVER_VERSION=0.19.1 bash ./verify_api_v2.sh http://127.0.0.1
```

确认 `server_version` 为 `0.19.1`。浏览器按 `Ctrl+F5` 强制刷新，然后检查：

1. 待办页面能正常上下滚动。
2. 设置页多人语音识别显示为完整功能卡和开关。
3. 新会议的章节时间来自真实字幕段锚点，不随会议总时长计算。

旧 v0.19.0 会议里已经保存的错误时间戳不会被程序猜测修改；需要从原字幕重新生成时间轴后才能得到真实时间。
