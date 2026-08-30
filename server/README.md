# ClearMeeting Server 2.0.0

ClearMeeting 是落叶 ESP32-S3 录音卡的自托管云服务。当前 `2.0.0 stable R1` 直接由已经验证的 `v0.21.0-upload-progress-r9` 重新编号，保持 `luoye-device-api/2`、10 MiB 范围补传和真实上传进度语义不变。

## 主要功能

- 账号、会议、设备、议程与待办按用户隔离。
- 一次性设备配对、可撤销设备令牌和绑定代次校验。
- 在线录音实时转写与翻译，离线录音幂等分片补传。
- 完整音频到达后执行整场规范转写、说话人识别和会议纪要。
- 按持久化音频范围的并集显示真实上传覆盖率和缺失时长。
- 网页提供会议历史、人员校正、议程待办、设备/SD 管理及 Word、Markdown、TXT 导出。

## 版本边界

| 项目 | 当前值 |
| --- | --- |
| 服务器 | `2.0.0` / `clearmeeting-server-v2.0.0` |
| 来源基线 | `v0.21.0-upload-progress-r9` |
| 设备协议 | `luoye-device-api/2` |
| 推荐固件 | `Luoye 2.0.0 stable-sdspi R1`（源自固件 1.7.2） |
| 设备鉴权 | `engineering` |

设备可通过 `GET /api/v2/build-info` 检查版本、协议和能力列表。API/2 工程鉴权并不等同于量产设备证书体系。

## 快速部署

```bash
cd deploy
cp .env.example .env
# 编辑 .env，填写独立强密钥、自己的 HTTPS Origin 和可选 AI Key
docker compose --profile real-asr up -d --build
bash check.sh
bash verify_api_v2.sh
```

部署前至少设置：

- `TEST_ACCOUNT_PASSWORD`
- `AUTH_SECRET`
- `DEVICE_API_SECRET`
- `CORS_ALLOW_ORIGINS`
- `DEEPSEEK_API_KEY`（需要翻译或 AI 纪要时）

完整升级流程见 [V2.0.0 部署说明](docs/DEPLOYMENT-V2.0.0.md)。

## 隐私与密钥

- 仓库只提供 `.env.example`，真实 `.env`、数据库、录音、密码、token 和 API Key 不进入 Git。
- `DEEPSEEK_API_KEY` 从运行环境读取，示例值为空；代码中没有内置密钥。
- 示例地址使用 `meeting.example.invalid` 或 RFC 5737 文档地址，部署时替换为自己的 HTTPS 域名。
- 不要把真实 `.env` 打进发布 ZIP；`deploy/package_server.ps1` 只打包示例配置。

## 开发验证

后端：

```powershell
cd server
python -m unittest discover -s tests -p "test_*.py"
```

前端：

```powershell
cd apps/web-client
pnpm install --frozen-lockfile
pnpm test
pnpm build
```

## 目录

```text
server/             FastAPI、SQLite、设备 API 与后台任务
apps/web-client/    React 19 / Vite 网页
deploy/             Docker Compose、nginx、环境模板和验证脚本
docs/               API、部署、升级和架构说明
```

技术栈：FastAPI · React 19 · Vite · SQLite · Docker Compose · nginx · FunASR · CAM++ · DeepSeek
