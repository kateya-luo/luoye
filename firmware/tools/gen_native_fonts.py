#!/usr/bin/env python3
"""Generate exact-size Regular font strikes required by the approved layout."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


FULL_CJK_SIZES = {16, 18, 20, 24}
EXTRA_NATIVE_SIZES = {8, 32}  # compact/version and legacy error-page helpers


def collect_runtime_characters(project: Path, layout: dict) -> str:
    chars = set()
    for page in layout["pages"]:
        for field in page["fields"]:
            if field.get("type", "text") != "image":
                chars.update(str(field.get("text", "")))

    # Include all user-visible C string literals. Comments are intentionally
    # excluded so the small strikes contain UI vocabulary, not source prose.
    string_re = re.compile(r'"(?:\\.|[^"\\])*"')
    for folder in (project / "main", project / "components"):
        for path in list(folder.rglob("*.c")) + list(folder.rglob("*.h")):
            source = path.read_text(encoding="utf-8", errors="ignore")
            for match in string_re.finditer(source):
                literal = match.group(0)[1:-1]
                chars.update(char for char in literal if ord(char) <= 0xFFFF)
    return "".join(sorted(chars))


def main() -> None:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout", required=True)
    parser.add_argument("--font", default="C:/Windows/Fonts/simsun.ttc")
    parser.add_argument(
        "--legacy-font16",
        default=str(project.parent / "recorder-card" / "assets" / "font16.bin"),
        help="original 2.13-inch 16px SimSun CMF1 strike",
    )
    parser.add_argument("--output", default=str(project / "assets"))
    parser.add_argument("--chars-file", default=str(project / "build" / "native_font_chars.txt"))
    args = parser.parse_args()

    layout_path = Path(args.layout)
    layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
    sizes = {
        int(round(field["size"]))
        for page in layout["pages"]
        for field in page["fields"]
        if field.get("type", "text") != "image"
    } | EXTRA_NATIVE_SIZES
    chars = collect_runtime_characters(project, layout)
    chars_file = Path(args.chars_file)
    chars_file.parent.mkdir(parents=True, exist_ok=True)
    chars_file.write_text(chars, encoding="utf-8")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    generator = Path(__file__).with_name("gen_font.py")
    for stale in output.glob("font_*.bin"):
        stale.unlink()
    legacy_output = output / "font16.bin"
    if legacy_output.exists():
        legacy_output.unlink()

    for size in sorted(sizes):
        if size == 16:
            legacy_source = Path(args.legacy_font16)
            if not legacy_source.is_file():
                raise SystemExit(f"legacy font16.bin not found: {legacy_source}")
            shutil.copyfile(legacy_source, legacy_output)
            continue
        font_size = size
        # SimSun's small FreeType strikes report a bitmap one row taller than
        # their nominal cell at these sizes.  Move the glyph up one row so the
        # bottom stroke is preserved instead of being clipped by the CMF1 cell.
        y_offset = -1 if size in {8, 9, 10, 11, 12, 14} else 0
        profile = "full" if size in FULL_CJK_SIZES else "subset"
        command = [
            sys.executable, str(generator),
            "--font", args.font,
            "--weight", "Regular",
            "--size", str(size),
            "--font-size", str(font_size),
            "--y-offset", str(y_offset),
            "--threshold", "128",
            "--monochrome",
            "--charset", profile,
            "--out", str(output / f"font_{size:02d}.bin"),
        ]
        if profile == "subset":
            command += ["--chars-file", str(chars_file)]
        subprocess.run(command, check=True)

    total = sum(path.stat().st_size for path in output.glob("font_*.bin")) + legacy_output.stat().st_size
    print(f"OK: native sizes={sorted(sizes)}, bytes={total}, chars={len(chars)}")


if __name__ == "__main__":
    main()
