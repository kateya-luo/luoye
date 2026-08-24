# ClearMeeting 1.0.1 部署说明

## 前提

- Linux 主机与 Docker Compose v2。
- 推荐至少 8 核 CPU、16 GB 内存，并为模型、录音和数据库准备独立持久化空间。
- 公网环境准备域名、HTTPS 证书和反向代理。

## 首次部署

```bash
cd deploy
cp .env.example .env
```

使用随机值替换示例密钥，例如：

```bash
openssl rand -hex 32
```

至少检查以下变量：

- `TEST_ACCOUNT_PASSWORD`
- `AUTH_SECRET`
- `DEVICE_API_SECRET`
- `CORS_ALLOW_ORIGINS`
- 模型路径、数据路径和 CPU/worker 配置

启动：

```bash
bash ./start.sh
```

离线转录默认方案按 5 分钟窗口组织。若主机同时承担在线字幕和其他业务，建议先以 4 个离线 worker 为上限，再根据实测负载调整。

## 验证

```bash
docker compose ps
curl http://localhost:34567/api/health
curl http://localhost:34567/api/v2/build-info
bash ./verify-device-api-v1.0.1.sh
```

`build-info` 应返回 `server_version=1.0.1` 和 `api_contract=luoye-device-api/2`。

## 数据与备份

`deploy/data/` 是运行数据，不进入 Git。升级前停止写入并备份 SQLite 数据库、录音/转录存储和 `.env`。不要只复制正在写入的单个数据库文件；优先使用 SQLite 在线备份方式或在服务停止后复制完整数据目录。

## 公网接入

不要直接把开发端口暴露到公网。使用 Nginx、Caddy 或同类反向代理终止 HTTPS，设置可信代理头、请求体限制、上传超时和访问日志脱敏。生产固件也应指向 HTTPS 端点。

## 回滚

保留上一版本镜像、部署文件和数据库备份。若新版本涉及数据库迁移，先确认迁移是否可逆；回滚时恢复匹配版本的服务端和数据快照，并再次运行设备 API 验证脚本。
