# 电量日志串口导出

固件通过 USB Serial/JTAG 提供只读命令，不把 SD 卡挂成电脑磁盘，也不会格式化、
删除或改写原有日志。

## 一键导出

1. 停止录音并用 USB 连接设备；
2. 双击 `EXPORT_POWER_CSV.bat`；
3. 如果电脑有多个串口，运行 `EXPORT_POWER_CSV.bat COM23`，把 COM23 换成实际端口；
4. 工具收到全部 Base64 数据块并通过长度、SHA-256 双重校验后，才会保存
   `power-YYYYMMDD-HHMMSS.csv`。

电脑需要 Python 和 pyserial。ESP-IDF 环境通常已经带有 pyserial；缺少时运行：

```powershell
python -m pip install pyserial
```

## 手工串口命令

用 115200 波特率打开串口后可以输入：

- `power_info`：只查看 `/diag/power.csv` 的大小和 SHA-256；
- `power_export`：输出带序号的 Base64 数据块；
- `power_help`：显示命令摘要。

录音过程中导出会返回 `recording_active`，防止读取 SD 卡影响录音写入。
