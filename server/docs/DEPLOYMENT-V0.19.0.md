# ClearMeeting v0.19.0 部署教程

本版本把滚动纪要改为“智能语义章节”：时间经过本身不会产生新时间戳；只有主题、目标、决策阶段或用户 MARK 等确实发生变化时，才建立新章节。同一主题的新细节继续更新当前章节。配套落叶 `v0.9.4` 会把当前章节显示在新的第 23 页，并仅在每个自然分钟边界刷新墨水屏。

升级只覆盖程序文件，不重装 Ubuntu、不清空数据库，也不覆盖现有 `deploy/.env`。

## 一、Windows 上传发布包

打开 Windows PowerShell，逐行执行：

```powershell
cd D:\OPENOP\files-mentioned-by-the-user-chatgpt\clearmeeting\releases

scp .\clearmeeting-server-v0.19.0-semantic-timeline-r1.zip deploy@192.0.2.10:/srv/clearmeeting/
scp .\clearmeeting-server-v0.19.0-semantic-timeline-r1.zip.sha256 deploy@192.0.2.10:/srv/clearmeeting/

ssh deploy@192.0.2.10
```

如果 `scp` 或 `ssh` 提示找不到命令，请在“管理员 PowerShell”安装 Windows OpenSSH 客户端：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

安装后关闭并重新打开普通 PowerShell，再从本节第一条命令开始执行。

## 二、Linux 校验并解压

成功进入服务器终端后，逐行执行：

```bash
cd /srv/clearmeeting
ls -lh clearmeeting-server-v0.19.0-semantic-timeline-r1.zip*

sed -i 's/\r$//' clearmeeting-server-v0.19.0-semantic-timeline-r1.zip.sha256
sha256sum -c clearmeeting-server-v0.19.0-semantic-timeline-r1.zip.sha256

rm -rf /srv/clearmeeting/clearmeeting-v0.19.0
mkdir -p /srv/clearmeeting/clearmeeting-v0.19.0
unzip -q clearmeeting-server-v0.19.0-semantic-timeline-r1.zip -d /srv/clearmeeting/clearmeeting-v0.19.0
cd /srv/clearmeeting/clearmeeting-v0.19.0
sha256sum -c SHA256SUMS.txt
```

两次校验都必须显示 `OK`。如果服务器没有 `unzip` 或 `rsync`：

```bash
sudo apt update
sudo apt install -y unzip rsync
```

## 三、备份现有数据

```bash
cd /srv/clearmeeting/clearmeeting
mkdir -p backups
cp -a server/data/clearmeeting.db "backups/clearmeeting-before-v0.19.0-$(date +%Y%m%d-%H%M%S).db"
ls -lh backups | tail
```

如果第一条 `cd` 提示目录不存在，请停止操作，不要猜测其他路径。

## 四、覆盖程序但保留数据库和配置

```bash
cd /srv/clearmeeting/clearmeeting

rsync -a --delete --exclude 'data/' --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.0/server/ server/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.0/apps/web-client/ apps/web-client/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.0/apps/card-sim/ apps/card-sim/
rsync -a --delete --exclude '.env' /srv/clearmeeting/clearmeeting-v0.19.0/deploy/ deploy/
rsync -a --delete /srv/clearmeeting/clearmeeting-v0.19.0/docs/ docs/
cp /srv/clearmeeting/clearmeeting-v0.19.0/package.json .
cp /srv/clearmeeting/clearmeeting-v0.19.0/package-lock.json .

test -f server/data/clearmeeting.db && echo '数据库仍在：OK'
test -f deploy/.env && echo '.env 仍在：OK'
```

最后两行必须都显示 `OK`。

## 五、更新版本标识

只修改版本项，不触碰密码和 DeepSeek Key：

```bash
cd /srv/clearmeeting/clearmeeting/deploy

grep -q '^SERVER_RELEASE=' .env \
  && sed -i 's/^SERVER_RELEASE=.*/SERVER_RELEASE=clearmeeting-server-v0.19.0/' .env \
  || echo 'SERVER_RELEASE=clearmeeting-server-v0.19.0' >> .env

grep -q '^ROLLING_SUMMARY_MIN_SEGMENTS=' .env \
  || echo 'ROLLING_SUMMARY_MIN_SEGMENTS=2' >> .env

grep -q '^DEVICE_ROLLING_SUMMARY_MAX_WAIT_SECONDS=' .env \
  || echo 'DEVICE_ROLLING_SUMMARY_MAX_WAIT_SECONDS=20' >> .env

grep -E '^(SERVER_RELEASE|ROLLING_SUMMARY_MIN_SEGMENTS|DEVICE_ROLLING_SUMMARY_MAX_WAIT_SECONDS)=' .env
```

语义章节边界由 DeepSeek 与服务端稳定器决定；上面 `20` 秒只是“字幕很少时何时至少评估一次”，不是固定切章周期。

## 六、重建并启动服务

```bash
cd /srv/clearmeeting/clearmeeting/deploy

docker compose --profile real-asr build --no-cache server nginx
docker compose --profile real-asr up -d funasr speaker server nginx
docker compose ps
```

等待 `server` 和 `speaker` 显示 healthy。然后查看日志：

```bash
docker compose logs --tail=120 server
docker compose logs --tail=60 nginx
```

## 七、验证版本和服务

```bash
curl -fsS http://127.0.0.1/api/v2/build-info
echo
curl -fsS http://127.0.0.1/health/ready
echo

EXPECTED_SERVER_VERSION=0.19.0 bash ./verify_api_v2.sh http://127.0.0.1
```

必须确认：

- `server_version` 为 `0.19.0`；
- `api_contract` 为 `luoye-device-api/2`；
- `/health/ready` 正常；
- 网页和录音卡原有账号、历史会议仍在。

## 八、真机验收智能章节

1. 开始一场会议，连续围绕同一主题说 2～3 分钟；网页当前章节内容可以更新，但不应因时间经过不断新增时间戳。
2. 明确切换到另一个议题，或按一次 MARK；语义成立后允许很快建立新章节。
3. 录音卡在取得首个章节后显示第 23 页；文字只在整分钟边界刷新，网络和 SD 录音仍持续运行。
4. 暂停、结束录音和错误页应立即切换，不等待分钟边界。
5. 两小时会议不应出现按 30 秒或 1 分钟机械切分的大量章节。

## 九、出现问题时回滚

先查看最新备份名：

```bash
cd /srv/clearmeeting/clearmeeting
ls -lt backups | head
```

停止服务并恢复数据库（把文件名替换成实际备份名）：

```bash
cd /srv/clearmeeting/clearmeeting/deploy
docker compose stop server nginx

cd /srv/clearmeeting/clearmeeting
cp -a backups/clearmeeting-before-v0.19.0-YYYYMMDD-HHMMSS.db server/data/clearmeeting.db
```

程序回滚时，重新解压并按第四节用上一版发布包覆盖，再执行：

```bash
cd /srv/clearmeeting/clearmeeting/deploy
docker compose --profile real-asr up -d funasr speaker server nginx
```
