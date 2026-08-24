#!/usr/bin/env python3
"""Read /diag/power.csv from Luoye over the USB serial port."""

from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import re
import sys
import time


BEGIN_PREFIX = "LY|SD_EXPORT|event=begin "
END_PREFIX = "LY|SD_EXPORT|event=end "
ERROR_PREFIX = "LY|SD_EXPORT|event=error "
DATA_RE = re.compile(r"^LY\|SD_EXPORT_DATA\|seq=(\d+)\|([A-Za-z0-9+/=]*)$")
KV_RE = re.compile(r"([a-zA-Z0-9_]+)=([^ ]+)")


def fields(line: str) -> dict[str, str]:
    return dict(KV_RE.findall(line))


def choose_port(explicit: str | None) -> str:
    if explicit:
        return explicit.upper()
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    preferred = [
        p for p in ports
        if p.vid in {0x303A, 0x10C4, 0x1A86} or
        "Espressif" in (p.manufacturer or "") or
        "USB JTAG" in (p.description or "")
    ]
    candidates = preferred or ports
    if len(candidates) == 1:
        return candidates[0].device
    if not candidates:
        raise RuntimeError("没有检测到串口，请连接设备后重试，或指定 COM 口。")
    names = ", ".join(f"{p.device} ({p.description})" for p in candidates)
    raise RuntimeError(f"检测到多个串口：{names}。请用参数指定，例如 COM23。")


def receive(port: str, output_dir: Path, start_timeout: float,
            transfer_timeout: float) -> Path:
    import serial

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.25
    ser.write_timeout = 3
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"已连接 {port}，正在请求 power.csv……")

    begin: dict[str, str] | None = None
    chunks: dict[int, bytes] = {}
    command_at = 0.0
    start_at = time.monotonic()
    transfer_at = start_at
    try:
        while True:
            now = time.monotonic()
            if begin is None and now - command_at >= 2.0:
                ser.write(b"power_export\r\n")
                ser.flush()
                command_at = now
            if begin is None and now - start_at > start_timeout:
                raise RuntimeError("设备没有响应导出命令；请确认烧录的是串口导出版固件。")
            if begin is not None and now - transfer_at > transfer_timeout:
                raise RuntimeError("串口传输超时。")

            raw_line = ser.readline()
            if not raw_line:
                continue
            line = raw_line.decode("ascii", errors="ignore").strip()
            if line.startswith(ERROR_PREFIX):
                info = fields(line)
                raise RuntimeError(
                    f"设备拒绝导出：{info.get('reason', info.get('result', 'unknown'))}"
                )
            if line.startswith(BEGIN_PREFIX):
                begin = fields(line)
                chunks.clear()
                transfer_at = now
                print(
                    f"开始接收：{begin.get('bytes', '?')} 字节，"
                    f"SHA-256 {begin.get('sha256', '?')}"
                )
                continue
            match = DATA_RE.match(line)
            if match and begin is not None:
                sequence = int(match.group(1))
                try:
                    chunks[sequence] = base64.b64decode(match.group(2), validate=True)
                except ValueError as exc:
                    raise RuntimeError(f"第 {sequence} 个数据块损坏。") from exc
                transfer_at = now
                if len(chunks) % 50 == 0:
                    print(f"已接收 {len(chunks)} 个数据块……")
                continue
            if line.startswith(END_PREFIX) and begin is not None:
                end = fields(line)
                if end.get("result") != "ok":
                    raise RuntimeError(f"设备导出失败：{end.get('reason', 'unknown')}")
                expected_chunks = int(end["chunks"])
                missing = [i for i in range(expected_chunks) if i not in chunks]
                if missing:
                    raise RuntimeError(f"串口丢失数据块，首个缺失序号：{missing[0]}")
                payload = b"".join(chunks[i] for i in range(expected_chunks))
                expected_size = int(begin["bytes"])
                if len(payload) != expected_size:
                    raise RuntimeError(
                        f"文件长度校验失败：收到 {len(payload)}，应为 {expected_size}。"
                    )
                digest = hashlib.sha256(payload).hexdigest()
                expected_digest = begin["sha256"].lower()
                if digest != expected_digest or digest != end["sha256"].lower():
                    raise RuntimeError("SHA-256 校验失败，文件未保存。")
                output_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                output = output_dir / f"power-{stamp}.csv"
                output.write_bytes(payload)
                print(f"导出完成：{output}")
                print(f"SHA-256：{digest}")
                return output
    finally:
        ser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="导出落叶录音卡的电量日志")
    parser.add_argument("port", nargs="?", help="串口，例如 COM23；不填则自动检测")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd(),
                        help="CSV 保存目录，默认当前目录")
    parser.add_argument("--start-timeout", type=float, default=30.0)
    parser.add_argument("--transfer-timeout", type=float, default=300.0)
    args = parser.parse_args()
    try:
        import serial  # noqa: F401
    except ImportError:
        print("缺少 pyserial，请执行：python -m pip install pyserial", file=sys.stderr)
        return 2
    try:
        port = choose_port(args.port)
        receive(port, args.output_dir, args.start_timeout, args.transfer_timeout)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
