# 电量离线诊断

本版本按 1000mAh、3.7V 单节聚合物锂电池设置 BQ25186：

- ICHG 请求值固定为 1000mA；
- 输入限流为 1050mA；
- BQ25186 负责 CC、CV、ITERM 和自动再充电；
- MAX17048 只用于估算电量，不再用它的 100% 提前停止充电；
- BQ25186 连续两次 5 秒采样报告 Charge Done 后，UI 显示 100%；
- USB 在位但尚未 Charge Done 时，UI 最高显示 99%。
- 放电显示使用 2026-08-20 实测曲线进行单调分段校准；
- 正常放电段最多每分钟追降 1%，低于 10% 或电压不高于 3.55V 时立即服从
  安全低电量，防止显示虚高。

实际电池充电电流可能因系统负载、输入限流、DPPM、VINDPM 或热调节而低于
1000mA。诊断时必须同时观察相应状态位。

## 离线日志

日志不依赖 Wi-Fi、账号或服务器。SD 成功挂载后，固件每 60 秒追加一行：

`/sdcard/diag/power.csv`

同一条摘要也会通过 USB 串口输出，前缀为：

`LY|POWER_DIAG|`

CSV 主要字段：

| 字段 | 含义 |
|---|---|
| `epoch_utc` | RTC/SNTP 可用时的 UTC 秒；未校时以 `uptime_s` 为准 |
| `recording` | 1 表示采样时正在写录音，可用于比较录音与待机掉电速度 |
| `gauge_soc` | MAX17048 原始 SOC，保留两位小数 |
| `filtered_soc` | 实测分段校准后，再进行三点中值滤波的 SOC |
| `displayed_soc` | 屏幕实际显示值 |
| `mv` | MAX17048 VCELL 换算的电池电压 |
| `usb` | 1 表示 BQ PG 检测到输入电源 |
| `charge_state` | 0=未充电，1=充电中，2=已充满 |
| `bq_phase` | 0=空闲，1=CC，2=CV，3=Charge Done |
| `ichg_ma` | BQ25186 ICHG 寄存器请求值，不等于瞬时实测电流 |
| `ilim_ma` | 输入限流设置 |
| `stat0/stat1` | BQ25186 原始状态和故障寄存器 |
| `max_config` | MAX17048 CONFIG/RCOMP |
| `max_hibrt` | MAX17048 休眠阈值 |
| `max_status` | MAX17048 STATUS |
| `max_version` | MAX17048 VERSION |
| `voltage_fallback` | 1 表示 SOC 读取失败，UI 使用了电压兜底 |

## 建议测试流程

1. 从低于 10% 开始，断开网络或不配置 Wi-Fi也可以；
2. 插入稳定的 5V、至少 2A 电源；
3. 充到 `bq_phase=3` 和 `charge_state=2`，确认屏幕显示 100%；
4. 拔掉 USB，按正常场景录音放电到低电安全收尾；
5. 保存完整 `power.csv`，不要只截取百分比；
6. 分析 CC/CV 时间、VCELL、原始 SOC、显示 SOC，以及 ILIM/DPPM/热调节状态。

此日志不能测量真实电流。若要区分电量计误差和真实功耗，仍需用 USB 功率计、
电源分析仪或串联电流表测量输入/电池电流。
