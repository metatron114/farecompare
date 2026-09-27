"""客运车站目录（权威来源：gaotie.com.cn 全国铁路客运车站大全）。

该目录只收录**办理客运**的车站，并附带车站等级/所属铁路局/途经线路，
因此可以用来剔除 12306 官方站名表里混入的纯货运、编组、乘降所性质车站
（例如 大连西、深圳西、广州西）。

用法：
    python tools/fetch_gaotie_stations.py             # 更新目录（索引页，快）
    python tools/fetch_gaotie_stations.py --cities    # 更新目录（含每城完整列表）
"""

from __future__ import annotations

import json
import os
import re
import time

from .paths import cache_file

CACHE = cache_file("passenger_city_stations.json")
BLOCKLIST = cache_file("blocked_stations.json")
TTL = 30 * 24 * 3600

# 行政区划后缀，用于把"大连市/阿坝藏族羌族自治州"规范成"大连/阿坝"
_ADMIN_SUFFIXES = (
    "藏族羌族自治州", "蒙古族藏族自治州", "哈尼族彝族自治州", "傣族景颇族自治州",
    "壮族苗族自治州", "土家族苗族自治州", "布依族苗族自治州", "彝族回族自治州",
    "藏族自治州", "彝族自治州", "白族自治州", "傣族自治州", "苗族自治州",
    "回族自治州", "蒙古族自治州", "朝鲜族自治州", "哈萨克自治州", "柯尔克孜自治州",
    "自治州", "自治县", "自治旗", "地区", "新区", "林区", "盟", "市", "县", "区",
)

_data: dict | None = None


def _norm_city(name: str) -> str:
    c = (name or "").strip()
    for suf in _ADMIN_SUFFIXES:
        if c.endswith(suf) and len(c) > len(suf):
            return c[: -len(suf)]
    return c


def load() -> dict:
    """载入客运目录。返回 {"stations": set, "cities": {城市:[车站]}, "source":..}"""
    global _data
    if _data is not None:
        return _data
    out = {"stations": set(), "cities": {}, "raw_cities": {}, "updated": 0,
           "source": "内置", "available": False, "blocked": set()}
    out["blocked"] = load_blocklist()
    try:
        if os.path.exists(CACHE):
            with open(CACHE, encoding="utf-8") as fh:
                obj = json.load(fh)
            out["stations"] = set(obj.get("station_names") or [])
            raw = obj.get("cities") or {}
            out["raw_cities"] = raw
            cities: dict[str, list[str]] = {}
            for city, names in raw.items():
                key = _norm_city(city)
                bucket = cities.setdefault(key, [])
                for n in names:
                    if n not in bucket:
                        bucket.append(n)
            out["cities"] = cities
            out["updated"] = obj.get("updated") or 0
            out["source"] = obj.get("source") or "gaotie.com.cn"
            out["available"] = bool(out["stations"])
    except Exception:
        pass
    _data = out
    return out


def is_passenger(name: str) -> bool:
    """车站名是否在客运目录中。目录不可用时返回 True（不做过滤）。"""
    d = load()
    if not d["available"]:
        return True
    return (name or "").strip() in d["stations"]


def is_blocked(name: str) -> bool:
    """是否被手工屏蔽。

    用于处理"目录里有、但实际买不到票/不需要出现"的车站。
    维护方式：直接编辑 cache/blocked_stations.json
        {"stations": ["大连西", "..."]}
    """
    d = load()
    return (name or "").strip() in d["blocked"]


def load_blocklist() -> set[str]:
    try:
        if os.path.exists(BLOCKLIST):
            with open(BLOCKLIST, encoding="utf-8") as fh:
                return set(json.load(fh).get("stations") or [])
    except Exception:
        pass
    return set()


def add_blocked(names: list[str]) -> int:
    """把车站加入屏蔽名单（幂等）。"""
    cur = load_blocklist()
    before = len(cur)
    cur.update(n.strip() for n in names if n and n.strip())
    os.makedirs(os.path.dirname(BLOCKLIST), exist_ok=True)
    tmp = BLOCKLIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"updated": time.time(), "stations": sorted(cur)}, fh, ensure_ascii=False)
    os.replace(tmp, BLOCKLIST)
    return len(cur) - before


def stations_of_city(city: str) -> list[str]:
    d = load()
    return list(d["cities"].get(_norm_city(city), []))


def stats() -> dict:
    d = load()
    return {"available": d["available"], "stations": len(d["stations"]),
            "cities": len(d["cities"]), "source": d["source"],
            "updated": d["updated"]}


def reload() -> dict:
    global _data
    _data = None
    return load()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    for name in ["大连", "大连北", "大连西", "深圳西", "广州西", "上海西", "北京南"]:
        print(f"  {name}: 客运目录={'是' if is_passenger(name) else '否'}")
    print("\n大连市车站:", stations_of_city("大连"))
