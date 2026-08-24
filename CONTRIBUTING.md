# 贡献说明

## 基本规则

- 不提交构建目录、依赖缓存、运行数据、录音、数据库、密钥或 `.env`。
- 修改固件协议时，同步更新 `firmware/docs/DEVICE_API_V2.md`、服务器实现和兼容矩阵。
- 固件变更至少运行仓库现有的 host/static 测试；发布前用 ESP-IDF 5.5.4 完整编译并在真机验证。
- 服务器变更至少运行 Python 单元测试，并验证 `/api/v2/build-info` 与设备端协议一致。
- PCB 或结构文件变更应同步记录源文件版本、导出日期、硬件修订号以及装配影响。
- 提交前运行 `powershell -ExecutionPolicy Bypass -File scripts/repo_audit.ps1`。

## 版本命名

固件和服务器独立发版，建议标签带组件前缀，例如 `firmware-v2.3.2` 和 `server-v1.0.1`。跨组件发布时必须更新 `docs/RELEASE_COMPATIBILITY.md`。

## 提交内容

一个提交尽量只解决一个清晰问题。提交说明应包含变更原因、验证方式，以及是否影响协议、存储格式、硬件或数据迁移。
