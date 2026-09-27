"""基础数据抓取（车站库 / 客运车站目录 / 民航城市 / 城市代码映射）。

全部为公开静态资源，HTTP 直取，无需登录。作为库函数供 CLI 调用：

    farecompare fetch stations      # 12306 官方站名表（3396 站）
    farecompare fetch gaotie        # 客运车站目录（5710 站，用于过滤货运站）
    farecompare fetch airports      # 民航通航城市（需本机 Edge）
    farecompare fetch citymap       # 城市 -> 民航三字码映射
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .paths import cache_dir, cache_file

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
GAOTIE_INDEX = "https://www.gaotie.com.cn/huochezhan/"


def _get(url: str, timeout: float = 30.0, retries: int = 2) -> str:
    import gzip
    import zlib
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
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


# ---------------------------------------------------------------------------
# 1) 12306 铁路车站库
# ---------------------------------------------------------------------------
def fetch_stations() -> dict:
    """更新 cache/stations.json（12306 官方站名表）。"""
    out = cache_file("stations.json")
    print("抓取 12306 官方站名表…")
    text = _get(STATION_URL)
    m = re.search(r"station_names\s*=\s*'(.*)'", text, re.S)
    body = m.group(1) if m else text
    rows = []
    for item in body.split("@"):
        p = item.split("|")
        if len(p) < 5:
            continue
        rows.append({"py": p[0], "name": p[1], "code": p[2],
                     "pinyin": p[3], "abbr": p[4]})

    # 合并旧缓存，避免新版表缺失的历史车站丢失
    old = []
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as fh:
                old = json.load(fh).get("stations") or []
        except Exception:
            old = []
    by_code = {r["code"]: r for r in old if r.get("code")}
    for r in rows:
        by_code.pop(r.get("code"), None)
    merged = rows + list(by_code.values())

    _dump(out, {"updated": time.time(), "source": "12306 官方站名表", "stations": merged})
    print(f"  共 {len(merged)} 个车站 -> {out}")
    return {"stations": len(merged), "path": out}


# ---------------------------------------------------------------------------
# 2) gaotie 客运车站目录（用于过滤货运站 + 城市分组 + slug）
# ---------------------------------------------------------------------------
CITY_MARK = re.compile(
    r"""class=['"]CHENGSHI['"][^>]*>\s*<a\s+href=['"](/chengshi/[^'"]+)['"][^>]*>\s*<b>([^<]+)</b>""")
COUNT_RE = re.compile(r"共\s*(\d+)\s*座车站")
STATION_RE = re.compile(r"""/chezhan/([a-z0-9]+)\.html['"][^>]*>([^<]{1,24})</a>""")


def _clean_name(name: str) -> str:
    n = (name or "").strip()
    return n[:-1] if n.endswith("站") and len(n) > 2 else n


def _base_slug(slug: str) -> str:
    """车站页 slug 带 "zhan"（dalianbeizhan），线路页不带（dalianbei）。"""
    s = (slug or "").strip()
    return s[:-4] if s.endswith("zhan") and len(s) > 4 else s


def _parse_stations(html: str) -> tuple[list[str], dict[str, str]]:
    names: list[str] = []
    slugs: dict[str, str] = {}
    for slug, raw in STATION_RE.findall(html):
        base = _clean_name(raw)
        if not base or len(base) > 20:
            continue
        if base not in names:
            names.append(base)
        slugs.setdefault(base, _base_slug(slug))
    return names, slugs


def fetch_gaotie_catalog(with_cities: bool = False, workers: int = 6) -> dict:
    """更新 cache/passenger_city_stations.json（客运车站目录）。"""
    out = cache_file("passenger_city_stations.json")
    print("抓取 gaotie 全国铁路客运车站目录…")
    html = _get(GAOTIE_INDEX)

    marks = list(CITY_MARK.finditer(html))
    cities: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    slugs: dict[str, str] = {}
    for i, m in enumerate(marks):
        path, city = m.group(1), m.group(2).strip()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        seg = html[m.end():end]
        cnt = COUNT_RE.search(seg)
        names, seg_slugs = _parse_stations(seg)
        slugs.update(seg_slugs)
        if city:
            cities[city] = names
            if cnt:
                counts[city] = int(cnt.group(1))
    print(f"  索引页：城市 {len(cities)} 个，车站 {sum(len(v) for v in cities.values())} 个，"
          f"slug {len(slugs)} 个")

    if with_cities and marks:
        print(f"  抓取 {len(marks)} 个城市页补齐列表（并发 {workers}）…")
        paths = [(m.group(1), m.group(2).strip()) for m in marks]

        def one(item):
            path, city = item
            try:
                page = _get("https://www.gaotie.com.cn" + path)
            except Exception:
                return city, [], {}
            return (city,) + _parse_stations(page)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for city, names, seg_slugs in pool.map(one, paths):
                slugs.update(seg_slugs)
                if names:
                    cities[city] = list(dict.fromkeys(cities.get(city, []) + names))

    all_names: set[str] = set()
    for v in cities.values():
        all_names.update(v)

    _dump(out, {
        "updated": time.time(),
        "source": "gaotie.com.cn 全国铁路客运车站大全",
        "city_count": len(cities),
        "station_count": len(all_names),
        "cities": cities,
        "counts": counts,
        "station_names": sorted(all_names),
        "slugs": slugs,
    })
    print(f"  客运车站 {len(all_names)} 个 / 城市 {len(cities)} 个 -> {out}")
    return {"stations": len(all_names), "cities": len(cities),
            "slugs": len(slugs), "path": out}


# ---------------------------------------------------------------------------
# 3) 民航城市（用 Edge 打开同程城市选择器抓取）
# ---------------------------------------------------------------------------
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def fetch_airports() -> dict:
    """更新 cache/airports.json（民航通航城市，需本机 Edge）。"""
    out = cache_file("airports.json")
    print("抓取民航城市（需要本机 Edge，通过同程城市选择器）…")
    try:
        from .browser import Browser
    except Exception as exc:
        raise RuntimeError(f"无法加载浏览器控制器：{exc}")

    date_str = time.strftime("%Y-%m-%d")
    url = f"https://www.ly.com/flights/itinerary/oneway/BJS-SHA?date={date_str}"

    cities: dict[str, str] = {}
    with Browser(headless=True) as b:
        sid = b.new_page()
        b.navigate(sid, url, wait=5)
        b.wait_for(sid, r"/\b[A-Z]{2}\d{3,4}\b/.test(document.body.innerText)", timeout=40)
        time.sleep(3)
        # 打开城市选择器
        b.eval(sid, """(() => {
          const els = document.querySelectorAll('div,span,a,input');
          for (const el of els) {
            const t = (el.innerText || el.value || '').trim();
            if (t === '北京' || t === '出发城市') { el.click(); return t; }
          }
          return null;
        })()""")
        time.sleep(3)
        read_js = r"""(() => {
          const txt = document.body.innerText || '';
          const pairs = {};
          for (const m of txt.matchAll(/([\u4e00-\u9fa5]{2,8})\s*([A-Z]{3})\b/g)) {
            if (!pairs[m[2]]) pairs[m[2]] = m[1];
          }
          return {count: Object.keys(pairs).length, data: pairs};
        })()"""
        res = b.eval(sid, read_js)
        cities.update(res.get("data") or {})
        for letter in LETTERS:
            try:
                b.eval(sid, """(() => {
                  const inputs = document.querySelectorAll('input[type=text], input:not([type])');
                  for (const el of inputs) {
                    const ph = (el.placeholder || '') + (el.getAttribute('aria-label') || '');
                    if (/城市|拼音|搜索|输入/.test(ph) || el.offsetParent !== null) {
                      el.focus();
                      const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                      setter.call(el, '%s');
                      el.dispatchEvent(new Event('input', {bubbles: true}));
                      el.dispatchEvent(new KeyboardEvent('keydown', {key: '%s', bubbles: true}));
                      el.dispatchEvent(new KeyboardEvent('keyup', {key: '%s', bubbles: true}));
                      return 'typed';
                    }
                  }
                  return 'no-input';
                })()""" % (letter, letter, letter))
                time.sleep(1.5)
                res = b.eval(sid, read_js)
                cities.update(res.get("data") or {})
            except Exception:
                continue
    if not cities:
        raise RuntimeError("未抓到民航城市（可能页面结构变化或浏览器不可用）")
    _dump(out, {"updated": time.time(), "source": "ly.com 城市选择器", "cities": cities})
    print(f"  民航城市 {len(cities)} 个 -> {out}")
    return {"cities": len(cities), "path": out}


# ---------------------------------------------------------------------------
# 4) 城市 -> 民航三字码映射
# ---------------------------------------------------------------------------
_ALIAS = {
    "HGH": ("杭州",), "CKG": ("重庆",), "NKG": ("南京",), "CGO": ("郑州",),
    "CSX": ("长沙",), "XMN": ("厦门",), "FOC": ("福州",), "TSN": ("天津",),
    "SHE": ("沈阳",), "DLC": ("大连",), "HRB": ("哈尔滨",), "CGQ": ("长春",),
    "TNA": ("济南",), "HFE": ("合肥",), "KHN": ("南昌",), "KMG": ("昆明",),
    "KWE": ("贵阳",), "NNG": ("南宁",), "HAK": ("海口",), "SYX": ("三亚",),
    "LHW": ("兰州",), "XNN": ("西宁",), "INC": ("银川",), "HET": ("呼和浩特",),
    "TYN": ("太原",), "SJW": ("石家庄",), "WNZ": ("温州",), "NGB": ("宁波",),
    "WUX": ("无锡",), "CZX": ("常州",), "XUZ": ("徐州",), "YNT": ("烟台",),
    "ZUH": ("珠海",), "SWA": ("汕头",), "KWL": ("桂林",), "LJG": ("丽江",),
    "LXA": ("拉萨",), "DYG": ("张家界",), "YIH": ("宜昌",), "XFN": ("襄阳",),
    "ZHA": ("湛江",), "MIG": ("绵阳",), "DNH": ("敦煌",), "KHG": ("喀什",),
}


def build_city_map() -> dict:
    """由民航城市库 + 内置机场表生成 cache/city_codes.json。"""
    from .airports import AIRPORTS
    from .stations import get_stations

    out = cache_file("city_codes.json")
    airports: dict[str, str] = {}
    try:
        with open(cache_file("airports.json"), encoding="utf-8") as fh:
            airports = json.load(fh).get("cities") or {}
    except Exception:
        pass

    mapping: dict[str, str] = {}
    for code, names in _ALIAS.items():
        for n in names:
            if code in airports or True:
                mapping[n] = code
    for city, info in AIRPORTS.items():
        cc = info.get("city_code")
        if cc:
            mapping[city] = cc

    st = get_stations()
    known_cities = list(st.city_stations.keys())
    unmatched = []
    for code, name in airports.items():
        if code in mapping.values():
            continue
        hit = None
        for city in known_cities:
            if name.startswith(city) and len(city) >= 2:
                if hit is None or len(city) > len(hit):
                    hit = city
        if hit:
            mapping.setdefault(hit, code)
        else:
            unmatched.append((code, name))

    _dump(out, {"mapping": mapping, "airports": airports, "unmatched": unmatched})
    print(f"  城市映射 {len(mapping)} 条 -> {out}")
    return {"mapping": len(mapping), "path": out}


def _dump(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fetch_stations()
