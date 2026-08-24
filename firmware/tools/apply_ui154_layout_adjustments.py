#!/usr/bin/env python3
"""Apply approved global placement rules to a Luoye 1.54-inch layout."""

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("layout")
    args = parser.parse_args()
    path = Path(args.layout)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["font"] = "SimSun"
    changed = 0
    icons = 0
    for page in data["pages"]:
        if page["id"] in ("11_todo_confirm", "12_todo_created"):
            page["fields"] = [field for field in page["fields"] if field.get("id") != "time"]
            for field in page["fields"]:
                if field.get("id") == "todo":
                    if page["id"] == "11_todo_confirm":
                        field.update({"x": 23, "y": 88, "width": 156, "height": 75,
                                      "size": 18, "lineHeight": 25, "maxLines": 3,
                                      "align": "center"})
                    else:
                        field.update({"x": 25, "y": 117, "width": 150, "height": 57,
                                      "size": 16, "lineHeight": 19, "maxLines": 3,
                                      "align": "center"})
        for field in page["fields"]:
            if (field.get("type") == "image" and field.get("y", 999) <= 10 and
                    field.get("x", 0) >= 130 and "16_41_52" in field.get("label", "")):
                field.update({"x": 147, "y": 0, "width": 22, "height": 22,
                              "aspect": 1, "lockAspect": True})
                icons += 1
            if re.fullmatch(r"battery_\d+", field.get("id", "")):
                if page["id"] == "20_charging":
                    field.update({
                        "x": 78,
                        "y": 136,
                        "width": 54,
                        "height": 16,
                        "size": 16,
                        "lineHeight": 14,
                        "maxLines": 1,
                        "align": "center",
                        "weight": 400,
                    })
                    continue
                field.update({
                    "x": 158,
                    "y": 4,
                    "width": 40,
                    "height": 17,
                    "size": 14,
                    "lineHeight": 14,
                    "maxLines": 1,
                    "align": "right",
                    "weight": 400,
                })
                changed += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: battery text={changed} (x=158 y=4), icons={icons} (x=147 y=0 22x22); todo due-time hidden")


if __name__ == "__main__":
    main()
