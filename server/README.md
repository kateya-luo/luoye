# ClearMeeting Server 1.0.1

ClearMeeting 是落叶录音卡的配套服务端，提供设备鉴权、实时字幕、断点上传、离线转录、会议整理、待办/日程同步和 Web 界面。当前推荐配套固件为 `2.3.2`，设备协议为 `luoye-device-api/2`。

## 主要能力

- 录音卡与网页端实时字幕。
- 10 MiB 范围上传、幂等提交、断点修复与会话恢复。
- 5 分钟离线转录窗口和可配置的 4 worker 并发。
- partial/final、说话人和纪要独立变化通道。
- 待办、日程、绑定、设备存储和录音管理。
- Docker Compose 部署、SQLite 数据与健康检查。

## Docker 快速启动

```bash
cd deploy
cp .env.example .env
```

编辑 `.env`，至少替换 `TEST_ACCOUNT_PASSWORD`、`AUTH_SECRET` 和 `DEVICE_API_SECRET`，并按实际域名设置 `CORS_ALLOW_ORIGINS`。然后执行：

```bash
bash ./start.sh
curl http://localhost:34567/api/health
bash ./verify-device-api-v1.0.1.sh
```

公网部署必须通过 HTTPS 反向代理对外提供服务。完整步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 本地开发与测试

Web 前端：

```bash
npm install
npm run dev:web
```

Python 服务端测试：

```bash
python -m pip install -r server/requirements.txt
cd server
python -m unittest discover -s tests -p "test_*.py"
```

## 目录

```text
deploy/       Docker Compose、示例环境变量和部署脚本
server/       Python API、任务调度与单元测试
web/          Web 前端
docs/         协议、部署、安全与验收文档
```

运行数据库、录音、缓存、`.env` 和模型文件不会进入 Git。发布前请同时检查根目录的安全策略与发布清单。
