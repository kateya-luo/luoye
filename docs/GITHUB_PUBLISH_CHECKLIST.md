# GitHub 首次发布检查清单

## 1. 确认发布边界

- 建议先创建 Private 仓库，完成团队复核后再决定是否公开。
- 若计划公开，先选择许可证；当前仓库没有 `LICENSE`，默认保留全部权利。
- 确认 PCB、器件库、字体、第三方依赖和模型文件允许按目标方式再分发。

## 2. GitHub 安全设置

- 启用 Secret Scanning 和 Push Protection。
- 启用 Private Vulnerability Reporting。
- 保护 `main` 分支，要求 Pull Request 和检查通过后合并。
- 限制 Actions 权限与仓库管理员数量。

## 3. 本地审计

```powershell
powershell -ExecutionPolicy Bypass -File scripts/repo_audit.ps1
git status --short --ignored
```

人工确认没有真实账号、内网地址、生产域名凭据、设备密钥、录音、数据库和客户数据。固件中当前工程服务器地址不是密钥，但公开或量产前仍应改成可配置且使用 HTTPS 的端点。

## 4. 初始化与首个提交

```powershell
git init -b main
git add .
git status --short
git commit -m "chore: prepare luoye monorepo baseline"
```

首次提交前应逐项审阅 `git status`；不要直接执行不经检查的全量推送。

## 5. 发布二进制附件

本地 `.github-release-assets/` 已被忽略。建议建立两个 GitHub Release：

- `firmware-v1.7.1`：上传固件 flash、symbols 及对应 SHA-256。
- `server-v0.21.0`：上传服务器 R9 发布包及对应 SHA-256。

不要把压缩包直接提交进 Git 历史。

当前 `luoye-fw-v1.7.1-engineering-wav-dma-r1` 包来自干净源码提交，
清单中的 `git_clean=true`，关键源码哈希、版本号和烧录镜像 SHA-256 均已核验。
在完成真机长录音和中断续传验收前，仍应作为 Engineering/Pre-release 发布。

## 6. 干净环境复验

在另一台电脑或临时目录中重新克隆仓库，按 README 完成一次固件构建、服务端测试、Docker 部署和真机连接。这样可以发现依赖于本机缓存、绝对路径或未提交文件的问题。
