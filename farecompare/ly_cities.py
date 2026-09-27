"""民航城市代码解析（数据来自 cache/city_codes.json 与 cache/airports.json）。

自动生成方式：python tools/fetch_airports3.py && python tools/build_city_map.py
"""

from __future__ import annotations

import json
import os

from .paths import cache_dir

CACHE = cache_dir()
CITY_CODES_PATH = os.path.join(CACHE, "city_codes.json")
AIRPORTS_PATH = os.path.join(CACHE, "airports.json")

_data: dict | None = None


def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    mapping: dict[str, str] = {}
    airports: dict[str, str] = {}
    try:
        with open(CITY_CODES_PATH, encoding="utf-8") as fh:
            obj = json.load(fh)
        mapping.update(obj.get("mapping") or {})
        airports.update(obj.get("airports") or {})
    except Exception:
        pass
    if not airports:
        try:
            with open(AIRPORTS_PATH, encoding="utf-8") as fh:
                airports.update(json.load(fh).get("cities") or {})
        except Exception:
            pass
    _data = {"mapping": mapping, "airports": airports}
    return _data


def city_code_of(city: str) -> str | None:
    """城市中文名 -> 民航城市三字码（如 杭州 -> HGH）。"""
    d = _load()
    c = (city or "").strip().rstrip("市")
    if not c:
        return None
    if c in d["mapping"]:
        return d["mapping"][c]
    for name, code in d["mapping"].items():
        if c in name or name in c:
            return code
    return None


def city_of_code(code: str) -> str | None:
    d = _load()
    up = (code or "").strip().upper()
    for name, c in d["mapping"].items():
        if c == up:
            return name
    return d["airports"].get(up)


def all_cities() -> dict[str, str]:
    """城市中文名 -> 三字码。"""
    return dict(_load()["mapping"])


def all_airports() -> dict[str, str]:
    """三字码 -> 名称（含机场名）。"""
    return dict(_load()["airports"])


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    m = all_cities()
    a = all_airports()
    print(f"城市映射 {len(m)} 条，机场条目 {len(a)} 条")
    for c in ["北京", "上海", "杭州", "重庆", "乌鲁木齐", "三亚", "拉萨", "喀什"]:
        print(f"  {c} -> {city_code_of(c)}")
    print("反查 HGH ->", city_of_code("HGH"), "| SYX ->", city_of_code("SYX"))
