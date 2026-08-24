#!/usr/bin/env python3
"""Render the approved 22-page layout with the exact firmware font algorithm."""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = HEIGHT = 200


def unpack_page(blob: bytes) -> Image.Image:
    if len(blob) != 5000:
        raise ValueError(f"invalid page asset length: {len(blob)}")
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if blob[y * 25 + x // 8] & (0x80 >> (x & 7)):
                pixels[x, y] = 0
    return image


class FontStrike:
    def __init__(self, path: Path):
        blob = path.read_bytes()
        if blob[:4] != b"CMF1":
            raise ValueError(f"font magic mismatch: {path}")
        self.cell, self.row_bytes, count, _ = struct.unpack_from("<HHII", blob, 4)
        self.row_bytes = self.row_bytes or (self.cell + 7) // 8
        glyph_bytes = self.cell * self.row_bytes
        cps = struct.unpack_from(f"<{count}H", blob, 16)
        base = 16 + count * 2
        self.glyphs = {
            cp: blob[base + i * glyph_bytes:base + (i + 1) * glyph_bytes]
            for i, cp in enumerate(cps)
        }

    def pixel(self, glyph: bytes, x: int, y: int) -> bool:
        return bool(glyph[y * self.row_bytes + x // 8] & (0x80 >> (x & 7)))


class FirmwareFont:
    def __init__(self, font_dir: Path):
        strikes = [FontStrike(path) for path in sorted(font_dir.glob("font_*.bin"))]
        legacy = font_dir / "font16.bin"
        if legacy.is_file():
            strikes.append(FontStrike(legacy))
        self.strikes = {strike.cell: strike for strike in strikes}
        if not self.strikes:
            raise ValueError(f"no native font strikes in {font_dir}")

    @staticmethod
    def advance(char: str, size: int) -> int:
        return (size + 1) // 2 if ord(char) < 0x100 else size

    def width(self, text: str, size: int) -> int:
        return sum(self.advance(char, size) for char in text)

    def draw(self, image: Image.Image, x: int, y: int, size: int, text: str) -> None:
        pixels = image.load()
        strike = self.strikes.get(size)
        if not strike:
            raise ValueError(f"missing native {size}px font strike")
        for char in text:
            glyph = strike.glyphs.get(ord(char))
            if glyph:
                for dy in range(size):
                    for dx in range(size):
                        black = strike.pixel(glyph, dx, dy)
                        px, py = x + dx, y + dy
                        if black and 0 <= px < WIDTH and 0 <= py < HEIGHT:
                            pixels[px, py] = 0
            x += self.advance(char, size)


def split_lines(font: FirmwareFont, field: dict, text: str) -> list[str]:
    width = int(round(field["width"]))
    size = int(round(field["size"]))
    max_lines = int(field.get("maxLines", 1))
    lines = [""]
    for char in text:
        if char == "\n":
            if len(lines) >= max_lines:
                break
            lines.append("")
            continue
        candidate = lines[-1] + char
        if lines[-1] and font.width(candidate, size) > width:
            if len(lines) >= max_lines:
                break
            lines.append(char)
        else:
            lines[-1] = candidate
    return lines[:max_lines]


def draw_field(image: Image.Image, font: FirmwareFont, field: dict) -> None:
    text = str(field.get("text", ""))
    x = int(round(field["x"]))
    y = int(round(field["y"]))
    width = int(round(field["width"]))
    size = int(round(field["size"]))
    line_height = int(round(field.get("lineHeight", size * 1.25)))
    for index, line in enumerate(split_lines(font, field, text)):
        line_width = font.width(line, size)
        tx = x
        if field.get("align") == "center":
            tx += (width - line_width) // 2
        elif field.get("align") == "right":
            tx += width - line_width
        font.draw(image, tx, y + index * line_height, size, line)


def main() -> None:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout", default=r"D:\OPENOP\Luoye_UI_Layout_Editor\final_layout_r2\luoye_ui_layout_FINAL.json")
    parser.add_argument("--output", default=str(project / "docs" / "UI154_FIRMWARE_NATIVE_FONT_PREVIEW.png"))
    args = parser.parse_args()

    layout = json.loads(Path(args.layout).read_text(encoding="utf-8-sig"))
    font = FirmwareFont(project / "assets")
    rendered = []
    page_dir = project / "docs" / "ui154_native_font_pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    for page in layout["pages"]:
        image = unpack_page((project / "assets" / "ui154" / f"{page['id']}.bin").read_bytes())
        for field in page["fields"]:
            if field.get("type", "text") != "image":
                draw_field(image, font, field)
        image.save(page_dir / f"{page['id']}.png")
        rendered.append((page["id"], image))

    columns, cell_w, cell_h = 4, 220, 226
    rows = (len(rendered) + columns - 1) // columns
    sheet = Image.new("L", (columns * cell_w, rows * cell_h), 255)
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 12)
    for index, (page_id, image) in enumerate(rendered):
        x = (index % columns) * cell_w + 10
        y = (index // columns) * cell_h
        sheet.paste(image, (x, y))
        draw.text((x, y + 202), page_id, fill=0, font=label_font)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    print(f"OK: {len(rendered)} pages -> {output}")


if __name__ == "__main__":
    main()
