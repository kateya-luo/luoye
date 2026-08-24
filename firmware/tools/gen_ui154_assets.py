#!/usr/bin/env python3
"""Convert the approved 200x200 Luoye UI delivery into firmware 1-bit assets.

The generated .bin files are exactly 5000 bytes. A set bit is a black pixel;
an unset bit is a white pixel. Pixels are packed MSB-first in display order.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH = 200
HEIGHT = 200
DEFAULT_THRESHOLD = 144

PAGE_TARGETS = [
    "01_home",
    "02_agenda",
    "03_device_status",
    "04_meeting_prepare",
    "05_meeting_caption",
    "06_meeting_status",
    "07_meeting_paused",
    "08_meeting_locked",
    "09_meeting_saving",
    "10_todo_listening",
    "11_todo_confirm",
    "12_todo_created",
    "13_schedule_reminder",
    "14_reminder_alt",
    "15_pair_hotspot",
    "16_wifi_connect",
    "17_network_ok",
    "18_bind_code",
    "19_bind_ok",
    "20_charging",
    "21_low_battery",
    "22_storage_error",
    "23_chapter_summary",
]


def decode_data_image(source: str) -> Image.Image:
    match = re.fullmatch(r"data:image/[^;]+;base64,(.+)", source, re.S)
    if not match:
        raise ValueError("invalid embedded icon data URI")
    return Image.open(io.BytesIO(base64.b64decode(match.group(1)))).convert("RGBA")


def compose_page(background_path: Path, page: dict) -> Image.Image:
    background = Image.open(background_path).convert("RGBA")
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), "white")
    canvas.alpha_composite(background.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS))
    for field in page["fields"]:
        if field.get("type") != "image":
            continue
        icon = decode_data_image(field["src"])
        size = (int(round(field["width"])), int(round(field["height"])))
        icon = icon.resize(size, Image.Resampling.LANCZOS)
        canvas.alpha_composite(icon, (int(round(field["x"])), int(round(field["y"]))))
    return canvas


def pack_1bpp(image: Image.Image, threshold: int) -> bytes:
    gray = image.convert("L")
    if gray.size != (WIDTH, HEIGHT):
        raise ValueError(f"expected {WIDTH}x{HEIGHT}, got {gray.size}")
    pixels = gray.load()
    out = bytearray(WIDTH * HEIGHT // 8)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if pixels[x, y] < threshold:
                out[y * (WIDTH // 8) + x // 8] |= 0x80 >> (x & 7)
    return bytes(out)


def unpack_preview(blob: bytes) -> Image.Image:
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if blob[y * (WIDTH // 8) + x // 8] & (0x80 >> (x & 7)):
                pixels[x, y] = 0
    return image


def main() -> None:
    default_source = "D:/OPENOP/Luoye_UI_Layout_Editor/backgrounds"
    default_layout = "D:/OPENOP/Luoye_UI_Layout_Editor/final_layout_r2/luoye_ui_layout_FINAL.json"
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=default_source)
    parser.add_argument("--layout", default=default_layout)
    parser.add_argument("--output", default=str(project / "assets" / "ui154"))
    parser.add_argument("--preview", default=str(project / "docs" / "UI154_APPROVED_ASSETS.png"))
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="black/white cutoff (0-255); lower values keep antialias fringes white and make text crisper",
    )
    args = parser.parse_args()

    if not 1 <= args.threshold <= 254:
        raise SystemExit("--threshold must be in the range 1..254")

    source = Path(args.source)
    layout_path = Path(args.layout)
    output = Path(args.output)
    layout_bytes = layout_path.read_bytes()
    layout = json.loads(layout_bytes.decode("utf-8-sig"))
    pages = layout.get("pages", [])
    page_ids = [page.get("id") for page in pages]
    if page_ids != PAGE_TARGETS:
        raise SystemExit(f"layout page order mismatch: {page_ids}")
    missing = [page_id for page_id in PAGE_TARGETS if not (source / f"{page_id}.png").is_file()]
    if missing:
        raise SystemExit(f"missing background pages: {missing}")

    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob("*.bin"):
        stale.unlink()
    records = []
    previews = []
    for page in pages:
        target = page["id"]
        src = source / f"{target}.png"
        composed = compose_page(src, page)
        blob = pack_1bpp(composed, args.threshold)
        destination = output / f"{target}.bin"
        destination.write_bytes(blob)
        records.append(
            {
                "id": target,
                "source": src.name,
                "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
        previews.append((target, unpack_preview(blob)))

    manifest = {
        "format": "luoye-ui154-1bpp-v1",
        "width": WIDTH,
        "height": HEIGHT,
        "stride": WIDTH // 8,
        "black_bit": 1,
        "threshold": args.threshold,
        "layout_file": layout_path.name,
        "layout_sha256": hashlib.sha256(layout_bytes).hexdigest(),
        "pages": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    columns = 4
    cell_w, cell_h = 220, 226
    rows = (len(previews) + columns - 1) // columns
    sheet = Image.new("L", (columns * cell_w, rows * cell_h), 255)
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(previews):
        x = (index % columns) * cell_w + 10
        y = (index // columns) * cell_h
        sheet.paste(image, (x, y))
        draw.text((x, y + 202), name, fill=0)
    preview_path = Path(args.preview)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(preview_path)
    print(f"OK: {len(records)} pages -> {output}")
    print(f"Preview: {preview_path}")


if __name__ == "__main__":
    main()
