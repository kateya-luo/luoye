# ClearMeeting Server 0.21.0 R9

ClearMeeting 是落叶 ESP32-S3 录音卡配套的自托管服务端与 Web 应用。当前版本与固件 1.7.0 R2 共同使用 `luoye-device-api/2`。

## 工作流程

1. 用户登录 ClearMeeting，并用一次性配对码绑定录音卡。
2. 设备先把 WAV 安全写入 microSD，同时上传可用的实时音频。
3. 断网或退出后，设备依据服务器缺口继续补传；服务器只确认完整、校验通过的范围。
4. 音频完整后，服务器执行整场离线转写和最终多人识别。
5. 用户在历史页选择模板生成会议纪要，并可编辑说话人、待办和导出内容。

## 0.21.0 R9 重点

- 补传进度直接显示服务器已经持久化确认的音频覆盖率。
- 按字节区间并集计算覆盖，稀疏文件和重复上传不会虚增百分比。
- 显示缺失音频时长、后台任务、排队数量和预计处理时间。
- 网页在线会议与录音卡会议统一经过整场 canonical ASR 和最终多人识别。
- 会议结束后由用户选择模板调用 DeepSeek 生成正式纪要。
- 保持 10 MiB 设备上传范围、幂等会话、缺口修复和账号隔离。

## 技术栈

FastAPI · React 19 · Vite · SQLite · Docker Compose · nginx · FunASR · CAM++ · DeepSeek

## API 边界

| 区域 | 路径 | 身份 |
| --- | --- | --- |
| 网页登录与账号数据 | `/api/v1/*` | 账号凭据或账号令牌 |
| 录音卡配对、会话、上传、字幕和议程 | `/api/v2/*` | 配对 nonce 或设备令牌 |
| 版本兼容检查 | `/api/v2/build-info` | 无 |
| 服务健康检查 | `/health`、`/health/ready` | 无 |

## 快速部署

```bash
cd deploy
cp .env.example .env
# 编辑 .env，设置独立密钥、密码、模型地址与允许的 CORS 来源
docker compose --profile real-asr --profile offline-asr-cpu up -d --build
bash check.sh
EXPECTED_SERVER_VERSION=0.21.0 bash ./verify_api_v2.sh http://127.0.0.1
```

升级、备份、验证和回退步骤见 [V0.21.0 部署说明](docs/DEPLOYMENT-V0.21.0.md)。

## 本地验证

```bash
python -m pip install -r server/requirements.txt
cd server
python -m unittest discover -s tests -p 'test_*.py'
```

```bash
npm ci
npm run build:web
```

## 安全提示

- 不要提交 `deploy/.env`、`server/.env`、`server/data/`、SQLite 数据库、录音或模型密钥。
- 发布包中的 `.env.example` 只允许包含占位值。
- 工程 HTTP 入口只用于隔离网络联调；真实数据必须部署 HTTPS。
- 数据库升级前先使用 SQLite backup API 生成一致性备份。
