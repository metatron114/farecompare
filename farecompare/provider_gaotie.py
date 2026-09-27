"""gaotie.com.cn 车次/票价适配器 —— 12306 之外的备用火车票数据源。

为什么需要它：
  12306 对短间隔连续请求会限流，做"多车站 + 多枢纽"的全面搜索时容易触顶。
  gaotie.com.cn 提供静态的线路页，直接给出两站之间的**全部车次 + 时刻 + 历时 + 票价**，
  无鉴权、无频率限制，非常适合承担大范围的线路搜索。

接口形态（实测）：
  https://www.gaotie.com.cn/lieche/{出发站slug}-{到达站slug}.html
  页面内每个车次形如：
      08:49 ⇢ G1251/G1254 ⇢ 19:11
      历时:10小时22分钟
      大连北站 ⇢ 经停17站 ⇢ 上海虹桥站
      距离:2054km
      二等座:¥966.5   一等座:¥1557   特等/商务座:¥3249

车站 slug 来自 cache/passenger_city_stations.json 的 slugs 字段
（如 大连北 -> dalianbei），由 tools/fetch_gaotie_stations.py 抓取。
"""

from __future__ import annotations

import gzip
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import zlib

from .paths import cache_file

CACHE = cache_file("passenger_city_stations.json")
ROUTE_CACHE = cache_file("gaotie_routes.json")
ROUTE_TTL = 6 * 3600
BASE = "https://www.gaotie.com.cn"

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": BASE + "/huochezhan/",
}

_lock = threading.Lock()
_slugs: dict[str, str] | None = None
_route_mem: dict[str, tuple[float, list[dict]]] = {}
_NEG_TTL = 1800          # 查到"该区间无车"也缓存 30 分钟，避免反复重查


# ---------------------------------------------------------------------------
# 车站 slug
# ---------------------------------------------------------------------------
def load_slugs() -> dict[str, str]:
    global _slugs
    if _slugs is not None:
        return _slugs
    slugs: dict[str, str] = {}
    try:
        with open(CACHE, encoding="utf-8") as fh:
            slugs = json.load(fh).get("slugs") or {}
    except Exception:
        slugs = {}
    _slugs = slugs
    return slugs


def slug_of(station_name: str) -> str | None:
    """车站名 -> 拼音 slug（去掉结尾的"站"后匹配）。"""
    if not station_name:
        return None
    s = slugs = load_slugs()
    name = station_name.strip()
    for cand in (name, name + "站", name.rstrip("站")):
        if cand in slugs:
            return slugs[cand]
    base = name[:-1] if name.endswith("站") and len(name) > 2 else name
    return slugs.get(base)


# ---------------------------------------------------------------------------
# 抓取与解析
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 20.0, retries: int = 2) -> str | None:
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))
    return None


def _strip(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"[ \t\u00a0]+", " ", t.replace("&nbsp;", " "))


_SEAT_MAP = {
    "二等座": "二等座", "一等座": "一等座", "特等/商务座": "商务座", "商务座": "商务座",
    "硬座": "硬座", "软座": "软座", "硬卧": "硬卧", "软卧": "软卧",
    "无座": "无座", "优选一等座": "优选一等座", "二等卧": "二等卧", "一等卧": "一等卧",
}

_TRAIN_BLOCK = re.compile(
    r"(\d{1,2}:\d{2})\s*⇢\s*([A-Z0-9/\-]{3,16})\s*⇢\s*(\d{1,2}:\d{2})\s*"
    r"(?:历时[:：]\s*([^\n]{2,24}?)\s*)?"
    r"([\u4e00-\u9fa5]{2,12}站)\s*⇢\s*经停\s*(\d+)\s*站\s*⇢\s*([\u4e00-\u9fa5]{2,12}站)?",
    re.S)


def _parse_route(html: str, want_from: str = "", want_to: str = "") -> list[dict]:
    """解析线路页，返回车次列表（含票价）。"""
    text = _strip(html)
    out: list[dict] = []
    # 以"时刻 ⇢ 车次 ⇢ 时刻"为主锚点切分，再在该片段内取票价
    anchors = list(re.finditer(
        r"(\d{1,2}:\d{2})\s*⇢\s*([A-Z0-9/\-]{3,16})\s*⇢\s*(\d{1,2}:\d{2})", text))
    for i, m in enumerate(anchors):
        dep, code, arr = m.group(1), m.group(2), m.group(3)
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        seg = text[m.end():end]
        # 历时
        dur_m = re.search(r"历时[:：]\s*([0-9]+)\s*小时\s*([0-9]+)\s*分", seg)
        dur_min = (int(dur_m.group(1)) * 60 + int(dur_m.group(2))) if dur_m else 0
        if not dur_min:
            dur_m2 = re.search(r"历时[:：]\s*([0-9]+)\s*分", seg)
            dur_min = int(dur_m2.group(1)) if dur_m2 else 0
        # 经停数与起终点
        stop_m = re.search(r"([\u4e00-\u9fa5]{2,12}站)\s*⇢\s*经停\s*(\d+)\s*站\s*⇢\s*([\u4e00-\u9fa5]{2,12}站)", seg)
        from_station = stop_m.group(1)[:-1] if stop_m else want_from
        to_station = stop_m.group(3)[:-1] if stop_m else want_to
        stops = int(stop_m.group(2)) if stop_m else 0
        dist_m = re.search(r"距离[:：]\s*([\d.]+)\s*km", seg)
        distance = float(dist_m.group(1)) if dist_m else 0.0
        # 票价：取该片段内所有"席别:¥价格"
        seats: dict[str, float] = {}
        for sm in re.finditer(r"([\u4e00-\u9fa5/]{2,8})\s*[:：]\s*[¥￥]\s*([\d.]+)", seg):
            label = _SEAT_MAP.get(sm.group(1).strip())
            if label:
                price = float(sm.group(2))
                if label not in seats or price < seats[label]:
                    seats[label] = price
        if not code or not seats:
            continue
        cheapest = min(seats.items(), key=lambda kv: kv[1])
        out.append({
            "code": code.split("/")[0],
            "code_full": code,
            "from_station": from_station,
            "to_station": to_station,
            "depart": dep,
            "arrive": arr,
            "duration_min": dur_min,
            "duration": (f"{dur_min // 60}小时{dur_min % 60:02d}分" if dur_min else "-"),
            "stops": stops,
            "distance_km": distance,
            "seats": seats,
            "seat": cheapest[0],
            "price": cheapest[1],
            "source": "gaotie",
            "source_label": "gaotie.com.cn",
        })
    return out


def route_trains(from_station: str, to_station: str,
                 use_cache: bool = True) -> dict:
    """查询两站之间的车次与票价。

    返回 {"trains": [...], "source": "gaotie", "page_found": bool,
          "url": ..., "error": None}

    page_found=True 表示页面存在（此时 trains 为空 = 该区间确实没有直达车），
    page_found=False 表示页面抓取失败/不存在（调用方可以考虑回落到 12306）。
    """
    f_slug, t_slug = slug_of(from_station), slug_of(to_station)
    if not f_slug or not t_slug:
        return {"trains": [], "source": "gaotie", "page_found": False, "error":
                f"缺少拼音标识：{from_station}({f_slug}) / {to_station}({t_slug})"}
    key = f"{f_slug}-{t_slug}"
    url = f"{BASE}/lieche/{key}.html"
    now = time.time()
    with _lock:
        hit = _route_mem.get(key)
        if use_cache and hit:
            age = now - hit[0]
            ttl = ROUTE_TTL if hit[1] else _NEG_TTL
            if age < ttl:
                return {"trains": hit[1], "source": "gaotie", "cached": True,
                        "page_found": True, "url": url,
                        "error": None if hit[1] else "该区间无直达车次"}
    html = _get(url)
    if not html:
        return {"trains": [], "source": "gaotie", "url": url, "page_found": False,
                "error": "抓取失败（网络异常）"}
    # 页面是否存在：正常线路页会带"列车时刻表票价"标题；404 页面则带 "404 Not Found"
    if "404" in html[:600] and "Not Found" in html[:600]:
        with _lock:
            _route_mem[key] = (now, [])
        return {"trains": [], "source": "gaotie", "url": url, "page_found": False,
                "error": "该区间没有线路页面"}
    trains = _parse_route(html, from_station, to_station)
    with _lock:
        _route_mem[key] = (now, trains)
    return {"trains": trains, "source": "gaotie", "url": url, "page_found": True,
            "error": None if trains else "该区间无直达车次（页面存在但无车次数据）"}


def stats() -> dict:
    return {"slugs": len(load_slugs()), "cached_routes": len(_route_mem)}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("slug 库:", stats())
    for a, b in [("大连北", "上海虹桥"), ("大连", "北京"), ("上海", "北京")]:
        t0 = time.time()
        res = route_trains(a, b)
        print(f"\n=== {a} → {b} ===  用时 {time.time()-t0:.1f}s  "
              f"车次 {len(res['trains'])}  error={res.get('error')}")
        for tr in res["trains"][:8]:
            print(f"   {tr['code']:<9s} {tr['depart']}-{tr['arrive']} {tr['duration']:>11s} "
                  f"{tr['from_station']}→{tr['to_station']}  经停{tr['stops']}  "
                  f"{tr['seat']} ¥{tr['price']:.1f}  {list(tr['seats'].items())[:3]}")
