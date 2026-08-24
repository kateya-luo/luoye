#!/usr/bin/env python3
"""Generate random-access monochrome CJK font strikes for Luoye firmware.

CMF1 format (little endian):
  0x00  char[4]  magic "CMF1"
  0x04  uint16   square glyph-cell size in pixels
  0x06  uint16   bytes per glyph row
  0x08  uint32   sorted glyph count
  0x0C  uint32   reserved
  0x10  uint16[] sorted BMP code points
  ...   packed glyph rows, MSB is the left-most pixel

The older font16.bin used the second uint16 as zero.  Firmware keeps backward
compatibility by deriving row_bytes from cell_size when it reads zero.
"""

import argparse
import bisect
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def codepoints(profile: str, extra_chars: str = ""):
    cps = list(range(0x20, 0x7F))
    if profile == "ascii":
        return cps
    if profile == "subset":
        cps += [ord(char) for char in extra_chars if ord(char) <= 0xFFFF]
        return sorted(set(cps))
    cps += [0xB7, 0xD7]
    cps += [0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2103]
    cps += list(range(0x3000, 0x3040))
    cps += list(range(0x4E00, 0x9FA6))
    cps += list(range(0xFF01, 0xFF5F))
    cps += [0xFFE5]
    return sorted(set(cps))


def render_glyph(font, cp, cell, row_bytes, x_offset, y_offset, threshold,
                 monochrome=False):
    ch = chr(cp)
    # The legacy 2.13-inch UI used Pillow's mode-1 FreeType path: direct
    # monochrome pixels with no antialiasing.  Keep that path selectable so a
    # strike can be reproduced exactly instead of thresholding gray pixels.
    img = Image.new("1" if monochrome else "L", (cell, cell), 0)
    ImageDraw.Draw(img).text((x_offset, y_offset), ch, font=font,
                             fill=1 if monochrome else 255)
    px = img.load()
    data = bytearray(cell * row_bytes)
    ink = False
    for y in range(cell):
        for x in range(cell):
            if bool(px[x, y]) if monochrome else px[x, y] >= threshold:
                data[y * row_bytes + (x >> 3)] |= 0x80 >> (x & 7)
                ink = True
    if not ink and cp not in (0x20, 0x3000):
        return None
    return bytes(data)


def load_bin(path):
    blob = Path(path).read_bytes()
    if blob[:4] != b"CMF1":
        raise ValueError("bad CMF1 magic")
    cell, row_bytes, count, _ = struct.unpack_from("<HHII", blob, 4)
    row_bytes = row_bytes or (cell + 7) // 8
    cps = struct.unpack_from(f"<{count}H", blob, 16)
    base = 16 + count * 2
    glyph_bytes = cell * row_bytes
    if base + count * glyph_bytes > len(blob):
        raise ValueError("truncated CMF1 file")
    return cps, blob, base, cell, row_bytes, glyph_bytes


def glyph_art(data, cell, row_bytes):
    lines = []
    for y in range(cell):
        lines.append("".join(
            "#" if data[y * row_bytes + (x >> 3)] & (0x80 >> (x & 7)) else "."
            for x in range(cell)
        ))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", default="C:/Windows/Fonts/NotoSansSC-VF.ttf")
    parser.add_argument("--weight", default="Regular")
    parser.add_argument("--size", dest="cell_size", type=int, default=16,
                        help="stored square glyph-cell size")
    parser.add_argument("--font-size", type=int,
                        help="FreeType size; defaults to the cell size")
    parser.add_argument("--x-offset", type=int, default=0)
    parser.add_argument("--y-offset", type=int, default=0)
    parser.add_argument("--threshold", type=int, default=96)
    parser.add_argument("--monochrome", action="store_true",
                        help="render directly to 1-bit pixels without antialiasing")
    parser.add_argument("--charset", choices=("full", "ascii", "subset"), default="full")
    parser.add_argument("--chars-file",
                        help="UTF-8 character source used by --charset subset")
    parser.add_argument("--out")
    parser.add_argument("--dump", help="read these glyphs back from --out")
    args = parser.parse_args()

    if args.dump:
        cps, blob, base, cell, row_bytes, glyph_bytes = load_bin(args.out)
        for ch in args.dump:
            index = bisect.bisect_left(cps, ord(ch))
            print(f"--- {ch} U+{ord(ch):04X} ---")
            if index >= len(cps) or cps[index] != ord(ch):
                print("(missing)")
                continue
            start = base + index * glyph_bytes
            print(glyph_art(blob[start:start + glyph_bytes], cell, row_bytes))
        return

    cell = args.cell_size
    if cell < 8 or cell > 64:
        raise ValueError("--size must be between 8 and 64")
    row_bytes = (cell + 7) // 8
    font = ImageFont.truetype(args.font, args.font_size or cell)
    if hasattr(font, "set_variation_by_name") and args.weight:
        try:
            font.set_variation_by_name(args.weight)
        except OSError:
            pass

    extra_chars = ""
    if args.charset == "subset":
        if not args.chars_file:
            raise ValueError("--charset subset requires --chars-file")
        extra_chars = Path(args.chars_file).read_text(encoding="utf-8")

    entries = []
    missing = 0
    for cp in codepoints(args.charset, extra_chars):
        glyph = render_glyph(font, cp, cell, row_bytes, args.x_offset,
                             args.y_offset, args.threshold, args.monochrome)
        if glyph is None:
            missing += 1
        else:
            entries.append((cp, glyph))

    output = Path(args.out or f"font{cell}.bin")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        stream.write(b"CMF1")
        stream.write(struct.pack("<HHII", cell, row_bytes, len(entries), 0))
        for cp, _ in entries:
            stream.write(struct.pack("<H", cp))
        for _, glyph in entries:
            stream.write(glyph)

    print(f"OK: {len(entries)} glyphs, cell={cell}, font={args.font_size or cell}, "
          f"profile={args.charset}, mono={args.monochrome}, "
          f"{output} ({output.stat().st_size / 1024:.0f} KB), "
          f"missing={missing}")


if __name__ == "__main__":
    main()
