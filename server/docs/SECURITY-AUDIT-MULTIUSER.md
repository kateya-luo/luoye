# ClearMeeting v0.12.0 账号与设备安全基线

## 安全边界

ClearMeeting 将“人”和“落叶设备”视为两种不同主体：

- 网页账号使用用户名/密码登录，获得账号 token。
- 落叶不保存账号密码，只在一次性配对后获得可撤销设备 token。
- 账号 token 不能调用设备业务身份，设备 token 也不能调用网页账号资源。
- 应用只暴露 `/api/v1`；旧 M1 `/api/devices/*` 和 `/device/*` 不属于 v0.12.0 部署面。

## 账号隔离

会议、录音、字幕、纪要、议程、待办和设备管理均必须由服务端根据已验证 principal 注入 `owner_user_id`。服务端不接受客户端在请求体中自报 owner。

跨账号查看、修改、删除、播放、导出、解绑或订阅实时状态应统一返回 `404`，避免泄露资源是否存在。

密码使用独立随机盐的 scrypt 保存。`AUTH_SECRET` 用于账号 token，不得与 `DEVICE_API_SECRET` 共用。

## 设备绑定与令牌

- 配对码是短时一次性凭据，设备同时持有 128-bit nonce。
- 网页只提交配对码，只有持有 nonce 的设备能领取绑定结果。
- 设备 token 为高熵 opaque token，数据库只保存其 SHA-256。
- 每个设备请求同时校验 token、`device_id`、过期时间、撤销状态和 `binding_generation`。
- 解绑/改绑使 `binding_generation` 递增并撤销旧 token；旧账号录音归属不会被改写。
- `DEVICE_API_SECRET` 若未通过环境变量给出，必须在 SQLite meta 生成并持久化；备份/迁移时数据库不可丢失。

## 幂等与内容校验

- 可重试写入必须使用稳定 `Idempotency-Key`。
- 录音分片使用会话、序号、offset、字节数和 SHA-256 校验。
- 相同幂等键只有在请求内容一致时才能重放为成功；键相同但内容不同必须返回冲突。
- 上传大小由 `DEVICE_MAX_CHUNK_BYTES` 和 `DEVICE_MAX_TODO_BYTES` 在服务端强制限制。

## CORS、HTTP 与 HTTPS

CORS 不是鉴权。`CORS_ALLOW_ORIGINS` 必须是逗号分隔的精确 Origin，不包含路径或通配子域；空值表示仅同源。带账号或设备 token 的公网环境不允许 `*`。

`192.168.31.183:80` 是局域网入口，当前的 `clearmeeting.chat:34567` 是端口转发后的公网工程入口。两者目前均为 HTTP，只可用于受控联调。正式账号、真实录音和量产设备 token 必须使用 HTTPS。

## 秘密配置规则

- 真实密码、`AUTH_SECRET`、`DEVICE_API_SECRET`、DeepSeek Key 和设备 token 只保存在 `deploy/.env`、SQLite 密文/摘要字段或专用密钥管理系统。
- 日志不得打印 Authorization header、配对 nonce、密码或完整设备 token。
- `.env` 不进入发布包或源码仓库，`.env.example` 只含占位符。
- 解绑、账号密码修改或怀疑泄露后，应撤销相关 token，不只是修改 CORS。

## 发布验收清单

- [ ] `bash deploy/verify_api_v1.sh <base-url>` 同时通过 `/health` 和 `/api/v1/build-info`。
- [ ] 源码、nginx 和文档中没有正式使用旧 `/api/devices/*` 或 `/device/*`。
- [ ] 账号 A 绑定后，账号 B 无法查看、改名、解绑或读取该设备数据。
- [ ] 配对码过期、nonce 错误、重复认领均被拒绝。
- [ ] 解绑后旧设备 token 立即失效，重新绑定后 `binding_generation` 递增。
- [ ] 相同会话创建或分片上传重放不产生重复数据，不同内容被拒绝。
- [ ] 服务器重启后绑定、上传确认点、议程 revision 和语音待办状态可恢复。
- [ ] 公网正式开放前已启用 HTTPS，并用最小化 CORS 列表替代工程 HTTP Origin。
