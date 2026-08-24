# 落叶（Luoye）录音卡

落叶录音卡是一套以 ESP32-S3、电子墨水屏和 ClearMeeting 服务为核心的录音、实时字幕、离线上传与会议整理系统。本仓库把固件、服务器、PCB 与机械结构放在同一版本基线中，便于协同开发和追踪兼容关系。

## 当前基线

| 组件 | 版本/标识 | 说明 |
| --- | --- | --- |
| 固件 | `2.3.2` | ESP-IDF 5.5.4，工程固件 |
| 服务器 | `1.0.1` | ClearMeeting 服务端与 Web 界面 |
| 设备 API | `luoye-device-api/2` | 固件与服务器的兼容边界 |
| 硬件 | `LY-HW-ENG-20260710` | 当前工程样机基线，尚未形成量产归档 |

兼容说明见 [发布兼容矩阵](docs/RELEASE_COMPATIBILITY.md)。

## 仓库结构

```text
firmware/             ESP32-S3 固件源码、烧录和诊断工具
server/               ClearMeeting 服务端、Web 前端与部署文件
hardware/pcb/         PCB 工程、BOM 和装配 STEP
hardware/mechanical/  外壳机械 STEP
docs/                 跨组件版本、制造与发布说明
scripts/              仓库发布前审计脚本
```

## 数据流

```text
麦克风/SD 卡 → ESP32-S3 固件 → luoye-device-api/2 → ClearMeeting
                  ↓                         ↓
              电子墨水屏              实时字幕/转录/待办
```

## 快速开始

- 固件编译、烧录和串口诊断：[firmware/README.md](firmware/README.md)
- 服务器部署与验证：[server/README.md](server/README.md)
- PCB 与机械资料：[hardware/README.md](hardware/README.md)
- 第一次发布前检查：[docs/GITHUB_PUBLISH_CHECKLIST.md](docs/GITHUB_PUBLISH_CHECKLIST.md)

## 版本和发布

源码提交中不放烧录包、服务端压缩包、运行数据库、录音或密钥。已验证的二进制包应作为 GitHub Release 附件上传，并建议分别使用组件标签：

- `firmware-v2.3.2`
- `server-v1.0.1`

本地候选附件保存在被忽略的 `.github-release-assets/` 目录中，不会进入 Git 历史。

## 工程状态

当前硬件资料属于工程样机，不等于可直接投产的制造包。PCB 工程、BOM、装配 STEP 和外壳 STEP 的导出日期并不完全一致；生产前必须完成 [制造归档检查](docs/MANUFACTURING_STATUS.md)。

## 许可

当前尚未选择开源许可证。在许可证文件正式加入仓库前，代码和设计资料默认保留全部权利。若仓库计划公开，请先确认自研代码、字体、器件库和第三方模型/依赖的再分发权利。
