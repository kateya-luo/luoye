#!/usr/bin/env python3
"""Merge selected UI pages without replacing already-approved pages."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--page", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    base = read_json(Path(args.base))
    source = read_json(Path(args.source))
    if base.get("schema") != "luoye-ui-layout/v3" or base.get("canvas") != [200, 200]:
        raise SystemExit("invalid base layout")
    if source.get("schema") != "luoye-ui-layout/v3" or source.get("canvas") != [200, 200]:
        raise SystemExit("invalid source layout")

    replacements = {page["id"]: page for page in source["pages"] if page["id"] in args.page}
    missing = sorted(set(args.page) - set(replacements))
    if missing:
        raise SystemExit(f"source pages missing: {missing}")
    seen = set()
    for index, page in enumerate(base["pages"]):
        if page["id"] in replacements:
            base["pages"][index] = replacements[page["id"]]
            seen.add(page["id"])
    missing = sorted(set(args.page) - seen)
    if missing:
        raise SystemExit(f"base pages missing: {missing}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: merged {', '.join(args.page)} -> {output}")


if __name__ == "__main__":
    main()
