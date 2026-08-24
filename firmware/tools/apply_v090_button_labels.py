#!/usr/bin/env python3
"""Apply the v0.9.0 three-key product vocabulary to a merged UI layout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


LABELS = {
    ("02_agenda", "footer"): "待办键 下一页",
    ("05_meeting_caption", "header"): "录音 12:36",
    ("05_meeting_caption", "footer"): "录音键暂停 · 长按结束",
    ("06_meeting_status", "footer"): "设置键 返回字幕",
    ("07_meeting_paused", "footer"): "录音键继续",
    ("08_meeting_locked", "unlock"): "长按设置键解锁",
    ("11_todo_confirm", "footer"): "待办确认 · 设置取消",
    ("13_schedule_reminder", "footer2"): "长按设置键推迟10分钟",
    ("16_wifi_connect", "footer"): "设置键 取消",
    ("17_network_ok", "footer"): "设置键 返回",
    ("18_bind_code", "footer"): "设置键 取消",
    ("19_bind_ok", "footer"): "设置键 返回主页",
    ("21_low_battery", "footer"): "设置键 返回主页",
    ("22_storage_error", "footer"): "设置键 返回主页",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("layout")
    args = parser.parse_args()
    path = Path(args.layout)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = set()
    for page in data["pages"]:
        for field in page["fields"]:
            key = (page["id"], field.get("id"))
            if key in LABELS:
                field["text"] = LABELS[key]
                changed.add(key)
    missing = sorted(set(LABELS) - changed)
    if missing:
        raise SystemExit(f"button-label fields missing: {missing}")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: applied {len(changed)} v0.9.0 button labels")


if __name__ == "__main__":
    main()
