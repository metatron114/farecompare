"""行程规划：直达 + 一次换乘中转 + 航铁联程，汇总火车/飞机候选方案。

中转策略（限定为**一次换乘**，即整个行程只经过一个中间车站）：
  1) 候选换乘城市 = 出发地/目的地走廊枢纽 + 全国主要枢纽，
     再按"绕行里程"排序（出发地→枢纽→目的地 相对直达里程的绕行量越小越优先），
     只取前 MAX_HUBS 个，避免把时间花在明显绕远的枢纽上。
  2) 每个枢纽逐站探测（呼和浩特的车只到北京北，不是北京南），
     前段到站必须等于后段发站，换乘间隔 30~360 分钟（跨夜自动 +24h）。
  3) **先配对时刻、后查票价**：只有时间上能衔接的组合才请求票价，请求数接近减半。

性能保障（解决"启动慢 / 响应慢"）：
  * Ticket12306 内置单次搜索的区间与票价缓存，同一 OD / 同一车次只请求一次；
  * 单次搜索有请求预算（服务端注入），超预算即提前收尾并如实告知；
  * 12306 对短间隔请求会返回 HTML 拦截页，故全程串行 + 1.2s 间隔 + 命中限流后全局冷却。

一次完整搜索（含中转）通常 20~60 秒，接口层提供进度回调，前端显示进度。
"""

from __future__ import annotations

import datetime
import threading

from .airports import city_distance_km
from .china_geo import HUB_TIER, capital_of, province_of, tier_of
from .provider_12306 import (Ticket12306, duration_text, normalize_duration,
                            ordered_stations, MAJOR_STATIONS, BudgetExceeded)
from .provider_flight import search_flights, deep_links
from .provider_gaotie import route_trains as gaotie_route, slug_of as gaotie_slug
from .stations import get_stations

HUBS: list[str] = [
    "郑州", "武汉", "西安", "长沙", "济南", "徐州", "南京", "合肥", "石家庄",
    "南昌", "杭州", "成都", "重庆", "广州", "沈阳", "太原", "兰州", "贵阳", "南宁", "福州",
]

# 城市走廊枢纽：小城市本身不是枢纽，但它接驳到本城最近的枢纽。
# 例如"呼和浩特 → 广州"的合理中转点是北京/郑州/西安，而不是郑州/武汉。
CORRIDOR_HUBS: dict[str, list[str]] = {
    "呼和浩特": ["北京", "郑州", "西安", "太原", "石家庄"],
    "包头": ["北京", "呼和浩特", "西安", "太原"],
    "鄂尔多斯": ["西安", "北京", "太原", "呼和浩特"],
    "银川": ["西安", "郑州", "兰州", "太原"],
    "西宁": ["兰州", "西安", "郑州", "成都"],
    "兰州": ["西安", "郑州", "西宁", "成都", "武汉"],
    "乌鲁木齐": ["兰州", "西安", "郑州", "西宁"],
    "拉萨": ["西宁", "兰州", "成都", "西安"],
    "哈尔滨": ["沈阳", "长春", "北京", "天津"],
    "长春": ["沈阳", "哈尔滨", "北京", "天津"],
    "沈阳": ["北京", "天津", "长春", "大连", "济南"],
    "大连": ["沈阳", "北京", "天津", "济南"],
    "太原": ["石家庄", "郑州", "西安", "北京"],
    "石家庄": ["郑州", "北京", "太原", "济南", "武汉"],
    "济南": ["徐州", "郑州", "北京", "南京", "天津"],
    "青岛": ["济南", "徐州", "南京", "郑州"],
    "烟台": ["济南", "青岛", "徐州"],
    "郑州": ["武汉", "西安", "徐州", "石家庄", "北京", "长沙"],
    "洛阳": ["郑州", "西安", "武汉"],
    "西安": ["郑州", "武汉", "兰州", "成都", "太原"],
    "武汉": ["郑州", "长沙", "南京", "合肥", "南昌", "西安"],
    "长沙": ["武汉", "广州", "南昌", "贵阳", "郑州"],
    "南昌": ["长沙", "武汉", "杭州", "福州", "合肥", "广州"],
    "合肥": ["南京", "武汉", "郑州", "杭州", "济南"],
    "南京": ["徐州", "合肥", "杭州", "上海", "济南", "武汉"],
    "上海": ["南京", "杭州", "徐州", "合肥", "济南"],
    "杭州": ["南京", "上海", "南昌", "合肥", "福州", "长沙"],
    "福州": ["杭州", "南昌", "厦门", "广州", "深圳"],
    "厦门": ["福州", "南昌", "广州", "深圳", "杭州"],
    "广州": ["长沙", "武汉", "深圳", "南宁", "南昌", "郑州"],
    "深圳": ["广州", "长沙", "武汉", "南昌", "南宁"],
    "南宁": ["广州", "贵阳", "长沙", "昆明", "深圳"],
    "桂林": ["长沙", "广州", "贵阳", "南宁"],
    "贵阳": ["长沙", "昆明", "重庆", "成都", "南宁", "武汉"],
    "昆明": ["贵阳", "成都", "重庆", "南宁", "长沙"],
    "成都": ["重庆", "西安", "贵阳", "昆明", "武汉", "兰州"],
    "重庆": ["成都", "武汉", "贵阳", "西安", "长沙"],
    "三亚": ["海口", "广州", "深圳"],
    "海口": ["广州", "深圳", "三亚", "南宁"],
    "徐州": ["郑州", "南京", "济南", "合肥", "上海"],
}

MIN_TRANSFER = 30       # 火车换火车最少间隔（分钟）
MAX_TRANSFER = 360      # 火车换火车最多间隔（分钟）
AIR_RAIL_MIN_GAP = 90   # 航铁联程最少间隔：机场到高铁站需额外时间
AIR_RAIL_MAX_GAP = 420  # 航铁联程最多间隔

# ---------------- 一次换乘（整个行程只经过一个中间车站）的参数 ----------------
MAX_HUBS = 4            # 单次搜索考察的换乘城市数（按绕行里程排序后取前 N）
HUB_STATION_TRY = 2     # 每个换乘城市最多探测的车站数
HUB_LEG_TOP = 5         # 每段候选车次数（按发车时刻取前 N）
HUB_LEG_PRICE_TOP = 5   # 每段实际查价车次数（按车次号去重）

# ---------------- 直达参数 ----------------
MAX_STATIONS = 4        # 每个城市最多取用的车站数（同城多站都要覆盖）
DIRECT_OD_CAP = 10      # 直达最多查询的 OD 组合数（gaotie 无限流，可放宽）
DIRECT_ENOUGH = 60      # 直达已抓到这么多车次就停止扩展 OD
DIRECT_PRICE_TOP = 12   # 直达整体查价名额（在各车站组合间轮流分配）
DIRECT_12306_CAP = 4    # gaotie 无数据时，最多回落到 12306 查询的区间数（省 12306 配额）

# 枢纽车站记忆：同一 OD 下探测成功的枢纽车站优先复用，减少重复请求
_STATION_CACHE: dict[str, list[str]] = {}
_STATION_CACHE_LOCK = threading.Lock()


def get_station_cache() -> dict[str, list[str]]:
    with _STATION_CACHE_LOCK:
        return {k: list(v) for k, v in _STATION_CACHE.items()}


def remember_station(key: str, station_name: str) -> None:
    with _STATION_CACHE_LOCK:
        lst = _STATION_CACHE.setdefault(key, [])
        if station_name in lst:
            lst.remove(station_name)
        lst.insert(0, station_name)
        del lst[4:]


# 枢纽城市枢纽站优先级（跨城市中转通常落在这些站）
HUB_STATION_PREF: dict[str, list[str]] = {
    "北京": ["北京南", "北京", "北京西", "北京北", "北京朝阳", "北京丰台"],
    "上海": ["上海虹桥", "上海", "上海南"],
    "广州": ["广州南", "广州", "广州东"],
    "郑州": ["郑州东", "郑州"],
    "武汉": ["武汉", "汉口", "武昌"],
    "西安": ["西安北", "西安"],
    "长沙": ["长沙南", "长沙"],
    "济南": ["济南西", "济南", "济南东"],
    "徐州": ["徐州东", "徐州"],
    "南京": ["南京南", "南京"],
    "合肥": ["合肥南", "合肥"],
    "石家庄": ["石家庄", "石家庄北"],
    "南昌": ["南昌西", "南昌"],
    "杭州": ["杭州东", "杭州"],
    "成都": ["成都东", "成都"],
    "重庆": ["重庆北", "重庆西", "重庆"],
    "沈阳": ["沈阳北", "沈阳"],
    "太原": ["太原南", "太原"],
    "兰州": ["兰州西", "兰州"],
    "贵阳": ["贵阳北", "贵阳"],
    "南宁": ["南宁东", "南宁"],
    "福州": ["福州", "福州南"],
    "深圳": ["深圳北", "深圳"],
    "天津": ["天津西", "天津"],
    "厦门": ["厦门北", "厦门"],
    "昆明": ["昆明南", "昆明"],
    "西宁": ["西宁"],
    "银川": ["银川"],
    "乌鲁木齐": ["乌鲁木齐"],
    "海口": ["海口东", "海口"],
}



def _time_to_min(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _leg_abs(leg: dict, day: int = 0) -> tuple[int, int] | None:
    """leg 在指定日期的 (发车绝对分钟, 到达绝对分钟)。

    "绝对分钟"以查询日 00:00 为 0 点连续计数，跨夜车次自然落在次日；
    这样两段车次的衔接判断就退化成一次减法，不必再对跨夜做特例处理。
    """
    dep = _time_to_min(leg.get("depart", ""))
    if dep < 0:
        return None
    dep += day * 1440
    return dep, dep + max(0, int(leg.get("duration_min") or 0))


def _connections(arr_abs: int, to_code: str, legs: list[dict],
                 lo: int = MIN_TRANSFER, hi: int = MAX_TRANSFER,
                 max_days: int = 3) -> list[tuple[dict, int, int]]:
    """从"上一腿到达"出发，找所有能衔接的下一腿。

    返回 [(leg, 发车绝对分钟, 换乘间隔分钟), ...]，按发车时间升序。

    要点：第 b 段车次每天都会重开一班，它的实际发车时刻是
        base + day * 1440   （day = 0,1,2,…）
    因此必须枚举**所有可能的日子**再去比较间隔，只试"到达当天/次日"会漏掉：
      * 上一条腿本身跨夜（如大连→上海 T131 历时 25 小时），到达已是 day+1，
        而真正能衔接的车在 day+2 出发；
      * 长距离卧铺 + 次日接续的情况。
    枚举上界由 **到达时刻** 决定：下一腿不可能早于到达（间隔 lo≥0），
    所以 day 只需遍历到 arr_abs // 1440 + 1。
    """
    out: list[tuple[dict, int, int]] = []
    if arr_abs < 0:
        return out
    last_day = min(max_days, arr_abs // 1440 + 1)
    for b in legs:
        if b.get("from_code") != to_code:
            continue
        base = _time_to_min(b.get("depart", ""))
        if base < 0:
            continue
        for day in range(last_day + 1):
            dep_abs = base + day * 1440
            gap = dep_abs - arr_abs
            if lo <= gap <= hi:
                out.append((b, dep_abs, gap))
                break          # 同一天只会匹配一次（间隔区间长度 < 1440 分钟）
    out.sort(key=lambda x: x[1])
    return out


# 枢纽通达度权重：按该城市在铁路网中的枢纽地位打分（0~5），
# 与"绕行里程"一起决定候选换乘城市的顺序——既顺路又四通八达的枢纽优先，
# 避免把请求花在顺路但没有接驳车次的城市上（例如 呼和浩特→长沙 无车）。
HUB_WEIGHT: dict[str, float] = {
    "北京": 5, "郑州": 5, "武汉": 5, "西安": 5, "上海": 5, "广州": 5,
    "南京": 4.5, "济南": 4.5, "长沙": 4.5, "徐州": 4.5, "杭州": 4.5,
    "成都": 4, "重庆": 4, "石家庄": 4, "合肥": 4, "南昌": 4, "天津": 4,
    "太原": 3.5, "沈阳": 3.5, "兰州": 3.5, "贵阳": 3.5, "南宁": 3.5,
    "福州": 3.5, "哈尔滨": 3, "长春": 3, "大连": 3, "青岛": 3,
    "深圳": 3, "昆明": 3, "厦门": 3, "西宁": 2.5, "银川": 2.5,
    "乌鲁木齐": 2.5, "呼和浩特": 2.5, "海口": 2,
}


def _hub_score(dep_city: str, arr_city: str, hub: str, direct_km: float) -> float:
    """综合评分：绕行越少越好，枢纽通达度越高越好。分数越小越优先。"""
    detour = city_distance_km(dep_city, hub) + city_distance_km(hub, arr_city) - direct_km
    weight = HUB_WEIGHT.get(hub, 2.0)
    # 通达度每高 1 分，相当于少绕行 180 km
    return detour - weight * 180.0


def _hub_stations(city: str, stations, limit: int = HUB_STATION_TRY) -> list[dict]:
    """枢纽候选站：优先枢纽站表，再补该城市其他主要站。"""
    names = stations.stations_of_city(city)
    pref = [n for n in HUB_STATION_PREF.get(city, []) if n in names]
    rest = [n for n in MAJOR_STATIONS.get(city, []) if n in names and n not in pref]
    ordered = pref + rest + [n for n in names if n not in pref and n not in rest]
    out: list[dict] = []
    for n in ordered:
        e = stations.by_name.get(n)
        if e:
            out.append({"name": n, "code": e["code"]})
        if len(out) >= limit:
            break
    return out


def _pick_stations(city: str, stations, limit: int = MAX_STATIONS) -> list[dict]:
    return ordered_stations(city, stations, limit)


def _od_plan(dep_stations: list[dict], arr_stations: list[dict],
             max_pairs: int) -> list[tuple[dict, dict]]:
    """生成 OD 组合，保证**每个出发站都有机会**被查询。

    按"车站优先级对角线"交错排列：先(主站,主站)，再(次站,主站)、(主站,次站)，
    这样即使组合数被上限截断，也不会出现"只查了某个出发站、另一个完全没查"的情况
    （例如 大连 与 大连北 都会覆盖到）。
    """
    pairs: list[tuple[dict, dict]] = []
    seen: set[tuple[str, str]] = set()
    n = max(len(dep_stations), len(arr_stations))

    def add(d: dict, a: dict) -> None:
        key = (d["code"], a["code"])
        if key not in seen and d["code"] != a["code"]:
            seen.add(key)
            pairs.append((d, a))

    for k in range(n):
        for i in range(len(dep_stations)):
            for j in range(len(arr_stations)):
                if i + j != k:
                    continue
                add(dep_stations[i], arr_stations[j])
    # 兜底：补齐剩余的出发站（保证每个出发站至少有一条组合）
    for d in dep_stations:
        if not any(p[0]["code"] == d["code"] for p in pairs):
            add(d, arr_stations[0])
    for a in arr_stations:
        if not any(p[1]["code"] == a["code"] for p in pairs):
            add(dep_stations[0], a)
    return pairs[:max_pairs]



class JourneyPlanner:
    def __init__(self, client: Ticket12306 | None = None, progress=None) -> None:
        self.client = client or Ticket12306()
        self.stations = get_stations()
        self.errors: list[str] = []
        self._progress = progress or (lambda *_: None)
        self.gaotie_note = ""
        # 接口返回的站点 map 常缺项，注入全量站名解析避免界面出现 "ESH" 这类电报码
        self.client.set_name_resolver(self.stations.name_of_code)

    def _say(self, msg: str, pct: int = 0) -> None:
        try:
            self._progress(msg, pct)
        except Exception:
            pass

    # ---------------- 底层 ----------------
    def _od_legs(self, from_code: str, to_code: str, date: str,
                 limit: int = HUB_LEG_TOP, allow_12306: bool = True) -> list[dict]:
        """查一个区间的车次（统一入口）。

        顺序：gaotie（无限流，含公布票价）→ 12306（实时票价，受请求预算限制）。
        返回统一结构的 leg 列表，按发车时刻排序。
        """
        legs, page_found = self._gaotie_legs(from_code, to_code, date)
        if legs:
            legs.sort(key=lambda l: _time_to_min(l["depart"]))
            return legs[:limit]
        if page_found or not allow_12306:
            return []
        try:
            rows = self._od_tickets(from_code, to_code, date)
        except BudgetExceeded:
            raise
        if not rows:
            return []
        rows.sort(key=lambda r: _time_to_min(r["depart"]))
        priced = self.client.query_price_many(rows[:limit], limit=limit)
        out = [l for l in (self._to_leg(r) for r in priced) if l]
        out.sort(key=lambda l: _time_to_min(l["depart"]))
        return out

    def _od_tickets(self, from_code: str, to_code: str, date: str) -> list[dict]:
        """查询区间余票；命中请求预算时向上抛出，由调用方决定收尾。"""
        try:
            rows = self.client.query_tickets(from_code, to_code, date)
        except BudgetExceeded:
            raise
        except Exception as exc:
            msg = str(exc)
            if msg not in self.errors:
                self.errors.append(msg)
            return []
        # 顺带学习客运站：车次里出现过的车站一定办理客运，用于过滤货运站
        if rows:
            codes = set()
            for r in rows:
                codes.add(r["from_code"])
                codes.add(r["to_code"])
            self.stations.learn_passenger(codes)
        return rows

    def _to_leg(self, rec: dict) -> dict | None:
        """把 12306 记录 + 票价转成统一 leg 结构（取最便宜的非无座席别）。"""
        prices = rec.get("prices") or []
        usable = [p for p in prices if not p.get("stand")]
        if not usable:
            return None
        best = usable[0]
        dur = normalize_duration(rec["duration"])
        return {
            "mode": "train",
            "code": rec["code"],
            "train_no": rec["train_no"],
            "from_station": rec["from_station"],
            "to_station": rec["to_station"],
            "from_code": rec["from_code"],
            "to_code": rec["to_code"],
            "depart": rec["depart"],
            "arrive": rec["arrive"],
            "duration_min": dur,
            "duration": duration_text(dur),
            "seat": best["seat"],
            "price": best["price"],
            "all_seats": [
                {"seat": p["seat"], "price": p["price"], "stand": p.get("stand", False)}
                for p in prices
            ],
            "bookable": rec.get("bookable", True),
        }

    def _gaotie_legs(self, from_code: str, to_code: str, date: str) -> tuple[list[dict], bool]:
        """用 gaotie 线路页查两站间的车次与票价（无频率限制，作为主搜索通道）。

        gaotie 给出的是**公布票价**（全票），不含实时折扣；实时折扣仍由 12306 提供，
        因此这里标注 source=gaotie，界面会显示数据来源。

        返回 (车次列表, 页面是否存在)：
          页面存在但无车次 = 该区间确实没有直达车 → 不需要回落 12306（省配额）；
          页面不存在/抓取失败才需要回落。
        """
        f_name = self.stations.name_of_code(from_code)
        t_name = self.stations.name_of_code(to_code)
        if not gaotie_slug(f_name) or not gaotie_slug(t_name):
            return [], False
        try:
            res = gaotie_route(f_name, t_name)
        except Exception as exc:
            self.gaotie_note = f"gaotie 查询失败：{str(exc)[:60]}"
            return [], False
        legs = []
        for tr in res.get("trains") or []:
            dur = tr.get("duration_min") or 0
            if dur <= 0 or dur > 48 * 60:
                continue
            # 站名以"请求时的站名"为准，避免同一电报码出现"乌鲁木齐/乌鲁木齐南"两种写法
            legs.append({
                "mode": "train",
                "code": tr["code"],
                "train_no": "",
                "from_station": f_name,
                "to_station": t_name,
                "from_code": from_code,
                "to_code": to_code,
                "from_no": "",
                "to_no": "",
                "depart": tr["depart"],
                "arrive": tr["arrive"],
                "duration_min": dur,
                "duration": tr.get("duration") or duration_text(dur),
                "seat": tr.get("seat") or "二等座",
                "price": float(tr.get("price") or 0),
                "all_seats": [{"seat": k, "price": v, "stand": k == "无座"}
                              for k, v in (tr.get("seats") or {}).items()],
                "bookable": True,
                "stops": tr.get("stops", 0),
                "source": "gaotie",
                "source_label": "gaotie.com.cn",
                "live": False,
            })
        return [l for l in legs if l["price"] > 0], bool(res.get("page_found"))

    # ---------------- 直达 ----------------
    def direct(self, dep_city: str, arr_city: str, date: str,
               include_train: bool = True, include_flight: bool = True,
               exclude_no_baggage: bool = True) -> dict:
        result = {"trains": [], "flights": [], "flight_note": "", "flight_source": "",
                  "flight_provider": "", "flight_live": False, "flight_calendar": [],
                  "excluded_no_baggage": [], "train_note": "", "links": {},
                  "train_sources": {}, "gaotie_note": ""}
        dep_stations = _pick_stations(dep_city, self.stations)
        arr_stations = _pick_stations(arr_city, self.stations)

        if include_train and dep_stations and arr_stations:
            pairs = _od_plan(dep_stations, arr_stations, DIRECT_OD_CAP)
            per_pair: list[list[dict]] = []
            placed = 0
            used_12306 = 0
            for i, (d, a) in enumerate(pairs, 1):
                self._say(f"查询直达 {d['name']} → {a['name']}", 4 + i * 4)

                # ① 先用 gaotie 查（无频率限制，覆盖全部车站组合）
                legs, page_found = self._gaotie_legs(d["code"], a["code"], date)
                if legs:
                    per_pair.append(legs)
                    continue
                if page_found:
                    # 页面存在但无车次 = 该区间确实没有直达车，无需回落 12306
                    continue

                # ② 页面缺失时才回落到 12306 实时接口（省配额）
                if used_12306 >= DIRECT_12306_CAP:
                    continue
                used_12306 += 1
                got = self._od_tickets(d["code"], a["code"], date)
                placed += self.client.last_placeholder or 0
                if got:
                    priced = self.client.query_price_many(got, limit=DIRECT_PRICE_TOP)
                    pair_legs = [l for l in (self._to_leg(r) for r in priced) if l]
                    if pair_legs:
                        per_pair.append(pair_legs)
                if sum(len(x) for x in per_pair) >= DIRECT_ENOUGH:
                    break

            result["train_placeholder"] = placed
            result["gaotie_note"] = self.gaotie_note

            # 把各车站组合的结果轮流取出，避免某一站的车次把名额占满
            picked: list[dict] = []
            round_idx = 0
            while len(picked) < DIRECT_PRICE_TOP and per_pair:
                progressed = False
                for rows in per_pair:
                    if round_idx < len(rows):
                        picked.append(rows[round_idx])
                        progressed = True
                        if len(picked) >= DIRECT_PRICE_TOP:
                            break
                if not progressed:
                    break
                round_idx += 1

            # 去重（同一车次可能被多个车站组合返回）
            seen: set[tuple] = set()
            for leg in picked:
                key = (leg["code"], leg["from_station"], leg["to_station"], leg["depart"])
                if key in seen:
                    continue
                seen.add(key)
                result["trains"].append(leg)
            src = {}
            for leg in result["trains"]:
                src[leg.get("source_label", "12306 实时")] = \
                    src.get(leg.get("source_label", "12306 实时"), 0) + 1
            result["train_sources"] = src
            result["train_note"] = "" if result["trains"] else self._train_note(
                placed, dep_city, arr_city, date)

        if include_flight:
            self._say(f"查询机票（{dep_city} → {arr_city}）", 32)
            fres = search_flights(dep_city, arr_city, date,
                                  exclude_no_baggage=exclude_no_baggage)
            result["flights"] = fres["flights"]
            result["flight_note"] = fres["note"]
            result["flight_source"] = fres["source"]
            result["flight_provider"] = fres.get("provider", "")
            result["flight_live"] = fres.get("live", False)
            result["flight_calendar"] = fres.get("calendar", [])
            result["excluded_no_baggage"] = fres.get("excluded_no_baggage", [])

        result["links"] = deep_links(dep_city, arr_city, date)
        result["trains"] = self._dedup_trains(result["trains"])
        result["flights"] = self._dedup_flights(result["flights"])
        return result

    @staticmethod
    def _train_note(placeholders: int, dep: str, arr: str, date: str) -> str:
        if placeholders > 0:
            return (f"12306 显示 {dep} → {arr} 在 {date} 的直达车次尚未起售"
                    f"（共 {placeholders} 条车次处于未到起售时间状态）。"
                    f"铁路车票按车站/车次分批放票，临近日期会陆续放票，建议换更近的日期或改查中转。")
        return (f"未查询到 {dep} → {arr} 在 {date} 的直达火车车次"
                f"（该区间当日可能无直达，或车票尚未起售）。")

    @staticmethod
    def _dedup_trains(legs: list[dict]) -> list[dict]:
        dedup: dict[tuple, dict] = {}
        for t in legs:
            key = (t["code"], t["from_code"], t["to_code"], t["depart"], t["seat"])
            if key not in dedup or t["price"] < dedup[key]["price"]:
                dedup[key] = t
        return sorted(dedup.values(), key=lambda x: (x["price"], x["duration_min"]))

    @staticmethod
    def _dedup_flights(rows: list[dict]) -> list[dict]:
        dedup: dict[tuple, dict] = {}
        for f in rows:
            key = (f.get("flight_no"), f.get("depart_time"))
            if key not in dedup or f.get("price", 1e9) < dedup[key].get("price", 1e9):
                dedup[key] = f
        return sorted(dedup.values(), key=lambda x: x.get("price", 1e9))

    # ---------------- 航铁联程 ----------------
    def air_rail(self, dep_city: str, arr_city: str, date: str,
                 hubs: int = 3, max_options: int = 4) -> list[dict]:
        """航铁联程：先飞到枢纽城市，再换高铁到目的地（或反向）。

        适用场景：小城市之间无直飞、火车耗时过长（如 呼和浩特 → 深圳）。
        组合规则：
          * 取走廊枢纽中"有民航机场"的城市
          * 去程：同程实时机票（已按无托运规则过滤）
          * 接续：12306 实时车票，当天换乘间隔 90~420 分钟（机场到高铁站需额外时间）
          * 只保留总价低于纯火车方案或明显省时的组合
        """
        from .provider_flight import search_flights
        from .ly_cities import city_code_of

        dep_stations = _pick_stations(dep_city, self.stations, 2)
        arr_stations = _pick_stations(arr_city, self.stations, 3)
        if not dep_stations or not arr_stations:
            return []

        # 候选枢纽：必须有民航机场
        cand: list[str] = []
        for h in self._hubs_for(dep_city, arr_city):
            if city_code_of(h):
                cand.append(h)
            if len(cand) >= hubs:
                break
        if not cand:
            return []

        options: list[dict] = []
        for i, hub in enumerate(cand):
            pct = 55 + int(30 * i / max(1, len(cand)))
            self._say(f"航铁联程：飞 {hub} 转高铁", pct)

            # 去程机票：出发地 -> 枢纽
            fres = search_flights(dep_city, hub, date, exclude_no_baggage=True, limit=6)
            flights = [f for f in (fres.get("flights") or []) if f.get("price")]
            if not flights:
                continue

            # 接续火车：枢纽 -> 目的地
            hub_stations = _hub_stations(hub, self.stations)
            rail_legs: list[dict] = []
            for hs in hub_stations:
                for a in arr_stations:
                    rows = self._od_tickets(hs["code"], a["code"], date)
                    if not rows:
                        continue
                    priced = self.client.query_price_many(
                        sorted(rows, key=lambda r: _time_to_min(r["depart"]))[:4],
                        limit=3)
                    for rec in priced:
                        leg = self._to_leg(rec)
                        if leg:
                            leg["from_hub_airport_hint"] = True
                            rail_legs.append(leg)
                    if rail_legs:
                        break
                if rail_legs:
                    break
            if not rail_legs:
                continue

            for f in flights:
                arr_min = _time_to_min(f.get("arrive_time") or "")
                if arr_min < 0:
                    continue
                if f.get("arrive_next_day"):
                    arr_min += 24 * 60
                for leg in rail_legs:
                    dep_min = _time_to_min(leg["depart"])
                    gap = dep_min - arr_min
                    if gap < AIR_RAIL_MIN_GAP or gap > AIR_RAIL_MAX_GAP:
                        continue
                    air_leg = {
                        "mode": "flight", "code": f["flight_no"],
                        "from_station": f.get("dep_airport") or dep_city,
                        "to_station": f.get("arr_airport") or hub,
                        "depart": f["depart_time"], "arrive": f["arrive_time"],
                        "duration_min": f.get("duration_min", 0),
                        "duration": f.get("duration", "-"),
                        "seat": f.get("cabin", "经济舱"), "price": f["price"],
                        "airline": f.get("airline", ""),
                    }
                    options.append({
                        "hub": hub,
                        "legs": [air_leg, leg],
                        "price": round(air_leg["price"] + leg["price"], 1),
                        "duration_min": (arr_min - _time_to_min(f["depart_time"]))
                        + gap + leg["duration_min"],
                        "transfer_min": gap,
                        "transfers": 1,
                        "air_rail": True,
                    })

        options.sort(key=lambda o: (o["price"], o["duration_min"]))
        trimmed: list[dict] = []
        per_hub: dict[str, int] = {}
        for o in options:
            if per_hub.get(o["hub"], 0) >= 2:
                continue
            per_hub[o["hub"]] = per_hub.get(o["hub"], 0) + 1
            trimmed.append(o)
            if len(trimmed) >= max_options:
                break
        for o in trimmed:
            o["duration"] = duration_text(o["duration_min"])
            o["transfer_text"] = f"{o['transfer_min']} 分钟"
            a, b = o["legs"]
            o["route_text"] = f"{dep_city} ✈ {a['to_station']} → 高铁 {b['to_station']}"
        return trimmed


    # ---------------- 中转 ----------------
    @staticmethod
    def _match_pairs(ins: list[dict], outs: list[dict]) -> list[tuple[dict, dict, int, bool]]:
        """两段车次配对，返回 (前段, 后段, 间隔分钟, 是否隔夜)。

        同日换乘：间隔 30~360 分钟，到站必须等于下一程发站。
        隔夜换乘：当天已无接续时，允许"前段 22:00 前到达 + 次日 06:00~12:00 出发"，
                  间隔按"到站到当晚 24:00 + 次日 0:00 到发车"计算，方便长途过夜中转。
        """
        cand_in = sorted(ins, key=lambda r: _time_to_min(r["depart"]))[:HUB_LEG_TOP]
        cand_out = sorted(outs, key=lambda r: _time_to_min(r["depart"]))[:HUB_LEG_TOP * 2]
        same_day: list[tuple[dict, dict, int, bool]] = []
        overnight: list[tuple[dict, dict, int, bool]] = []
        for a in cand_in:
            if a["to_code"] is None:
                continue
            arr_min = _time_to_min(a["arrive"])
            if arr_min < 0:
                continue
            dep_min_a = _time_to_min(a["depart"])
            crosses = arr_min < dep_min_a          # 前段本身跨夜
            for b in cand_out:
                if a["to_code"] != b["from_code"]:
                    continue
                dep_min = _time_to_min(b["depart"])
                if dep_min < 0:
                    continue
                gap = dep_min - arr_min
                if crosses:
                    gap += 24 * 60
                if MIN_TRANSFER <= gap <= MAX_TRANSFER:
                    same_day.append((a, b, gap, False))
                elif arr_min <= 22 * 60 and 6 * 60 <= dep_min <= 12 * 60:
                    # 隔夜：到站后住一晚，次日早晨再出发
                    night_gap = (24 * 60 - arr_min) + dep_min
                    if 8 * 60 <= night_gap <= 20 * 60:
                        overnight.append((a, b, night_gap, True))
        # 同日优先；同日无解时才给出隔夜方案
        return same_day if same_day else overnight

    def _has_pair(self, ins: list[dict], outs: list[dict]) -> bool:
        return bool(self._match_pairs(ins, outs))

    def _city_candidates(self, name: str) -> list[str]:
        """把用户输入归一到"可能的城市名"列表。

        用户可能输入车站名（瓦房店、金州、上海虹桥），也可能输入城市名（大连）。
        依次尝试：输入的站名本身 -> 该站的行政区归属城市 -> 去掉方位后缀。
        """
        out: list[str] = []
        n = (name or "").strip()

        def push(v: str) -> None:
            if v and v not in out:
                out.append(v)

        push(n)
        e = self.stations.by_name.get(n)
        if e:
            push(e.get("admin_city") or "")
            push(e.get("city") or "")
        base = n
        for suf in ("东", "南", "西", "北", "新", "站"):
            if base.endswith(suf) and len(base) > len(suf):
                push(base[: -len(suf)])
        return out

    def _hubs_for(self, dep_city: str, arr_city: str, limit: int = MAX_HUBS) -> list[str]:
        """候选换乘城市：走廊枢纽 + 全国主要枢纽 + **出发地/目的地的省会**，
        再按"绕行里程 + 枢纽等级"综合排序。

        偏僻站点（如 瓦房店、金州、喀什）本身不是枢纽，它们的合理中转点是
        所在省会（沈阳、乌鲁木齐）或大型铁路枢纽，因此省会一定会进入候选。

        评分 = 绕行里程 − 枢纽等级×300km − 走廊加成 100km（分数越小越优先）
        """
        ordered: list[str] = []
        seen: set[str] = set()

        def add(city: str) -> None:
            if city and city not in (dep_city, arr_city) and city not in seen:
                seen.add(city)
                ordered.append(city)

        side_a = CORRIDOR_HUBS.get(dep_city, [])
        side_b = CORRIDOR_HUBS.get(arr_city, [])
        for i in range(max(len(side_a), len(side_b))):
            if i < len(side_a):
                add(side_a[i])
            if i < len(side_b):
                add(side_b[i])

        # 出发地/目的地所在的省会优先（偏僻站点的关键中转点）
        # 注意：用户输入可能是车站名（瓦房店/金州），先归一到所属城市再找省会
        caps: list[str] = []
        for city in (dep_city, arr_city):
            for c in self._city_candidates(city):
                cap = capital_of(c)
                if cap:
                    caps.append(cap)
                    break
        # 同省的主要枢纽也纳入（如 辽宁 -> 沈阳/大连）
        provs = set()
        for city in (dep_city, arr_city):
            for c in self._city_candidates(city):
                p = province_of(c)
                if p:
                    provs.add(p)
                    break
        for prov in provs:
            for h in HUB_TIER:
                if province_of(h) == prov:
                    caps.append(h)
        for cap in caps:
            add(cap)

        for h in HUBS:
            add(h)
        for h in sorted(HUB_TIER, key=lambda c: -HUB_TIER[c]):
            add(h)

        direct = self._dist(dep_city, arr_city)
        corridor = set(side_a) | set(side_b)

        def score(city: str) -> float:
            detour = (self._dist(dep_city, city) + self._dist(city, arr_city) - direct)
            s = detour - tier_of(city) * 300.0
            if city in corridor or city in caps:
                s -= 100.0
            return s

        ordered.sort(key=score)
        return ordered[:limit]

    def _dist(self, a: str, b: str) -> float:
        """两个"输入名"之间的距离：先归一到有坐标的城市名再算，避免坐标缺失失真。

        例如 瓦房店 本身没有坐标，会先归一到 大连 再计算距离。
        """
        best = 9999.0
        for ca in self._city_candidates(a):
            for cb in self._city_candidates(b):
                d = city_distance_km(ca, cb)
                if d < best:
                    best = d
        return best

    def transfer(self, dep_city: str, arr_city: str, date: str,
                 max_options: int = 6, base_pct: int = 40, span_pct: int = 55,
                 hub_limit: int = MAX_HUBS, direct_best: float | None = None,
                 direct_fastest: int | None = None) -> list[dict]:
        """一次换乘中转：整个行程只经过一个中间车站。

        流程（先配对时刻、后查票价，请求数接近减半）：
          1) 按绕行里程取前 hub_limit 个换乘城市；
          2) 逐站探测"出发地→枢纽站"与"枢纽站→目的地"的余票；
          3) 仅对时间上能衔接（30~360 分钟）的组合请求票价，合并为方案；
          4) 质量门：与直达相比既贵很多又慢很多的组合直接丢弃
             （实测一次换乘仅在少数线路上更便宜，避免用"劣质中转"淹没结果）。

        direct_best / direct_fastest：直达最低价与最短耗时，用于质量门判断。
        """
        dep_stations = _pick_stations(dep_city, self.stations, 3)
        arr_stations = _pick_stations(arr_city, self.stations, 3)
        if not dep_stations or not arr_stations:
            return []
        hubs = self._hubs_for(dep_city, arr_city, hub_limit)
        if not hubs:
            return []

        options: list[dict] = []
        steps = max(1, len(hubs))
        dep_code, arr_code = dep_stations[0]["code"], arr_stations[0]["code"]
        hub_station_cache = get_station_cache()
        budget_hit = False

        for hi, hub in enumerate(hubs):
            if budget_hit:
                break
            pct = base_pct + int(span_pct * hi / steps)
            candidates = _hub_stations(hub, self.stations)
            if not candidates:
                continue
            good = hub_station_cache.get(f"{dep_code}-{arr_code}-{hub}", [])
            if good:
                candidates.sort(key=lambda x: (x["name"] not in good,))

            for hs in candidates:
                self._say(f"中转：经 {hub}（{hs['name']}）", pct)

                # 1) 前段：出发地 -> 枢纽站
                ins: list[dict] = []
                for d in dep_stations:
                    try:
                        rows = self._od_legs(d["code"], hs["code"], date, limit=HUB_LEG_TOP)
                    except BudgetExceeded:
                        budget_hit = True
                        break
                    if rows:
                        ins = rows
                        break
                if budget_hit or not ins:
                    continue

                # 2) 后段：枢纽站 -> 目的地（逐个到达站尝试）
                pairs: list[tuple[dict, dict, int, bool]] = []
                for a in arr_stations:
                    try:
                        outs = self._od_legs(hs["code"], a["code"], date, limit=HUB_LEG_TOP * 2)
                    except BudgetExceeded:
                        budget_hit = True
                        break
                    if not outs:
                        continue
                    found = self._match_pairs(ins, outs)
                    if found:
                        pairs = found
                        break
                if budget_hit or not pairs:
                    continue

                # 3) 剪枝：直达已存在且该枢纽组合的耗时远超直达时，不再花请求查价
                if direct_best is not None and direct_fastest:
                    fastest_legs = min((a["duration_min"] + b["duration_min"] + g)
                                       for a, b, g, _o in pairs)
                    if fastest_legs > direct_fastest * 2.5 and direct_best <= 1500:
                        self._say(f"中转：经 {hub} 明显慢于直达，跳过", pct + 3)
                        continue

                remember_station(f"{dep_code}-{arr_code}-{hub}", hs["name"])
                self._say(f"中转：经 {hub} 配对成功，生成方案", pct + 4)

                for a, b, gap, overnight in pairs:
                    hub_name = a["to_station"] or hub
                    options.append({
                        "hub": hub_name,
                        "legs": [a, b],
                        "price": round(a["price"] + b["price"], 1),
                        "duration_min": a["duration_min"] + gap + b["duration_min"],
                        "transfer_min": gap,
                        "transfers": 1,
                        "overnight": overnight,
                    })
                if options:
                    break            # 该枢纽已有可用方案，换下一个枢纽

        if budget_hit:
            self.errors.append("为保证响应速度，中转查询已达到本次请求上限并提前结束")

        # 质量门：丢掉"既不省钱、又明显更慢"的中转
        if direct_best is not None:
            keep = []
            for o in options:
                cheaper = o["price"] < direct_best - 5
                not_much_slower = (not direct_fastest) or (o["duration_min"] <= direct_fastest + 180)
                if cheaper or not_much_slower:
                    keep.append(o)
            options = keep

        options.sort(key=lambda o: (o["price"], o["duration_min"]))
        trimmed: list[dict] = []
        per_hub: dict[str, int] = {}
        for o in options:
            if per_hub.get(o["hub"], 0) >= 2:
                continue
            per_hub[o["hub"]] = per_hub.get(o["hub"], 0) + 1
            trimmed.append(o)
            if len(trimmed) >= max_options:
                break
        for o in trimmed:
            o["duration"] = duration_text(o["duration_min"])
            o["transfer_text"] = f"{o['transfer_min']} 分钟" + ("（隔夜）" if o.get("overnight") else "")
            o["route_text"] = f"{o['legs'][0]['from_station']} → {o['hub']} → {o['legs'][1]['to_station']}"
        return trimmed


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    day = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    planner = JourneyPlanner(progress=lambda m, p: print(f"  [{p:3d}%] {m}"))
    print(f"日期 {day}")
    d = planner.direct("北京", "上海", day)
    print(f"\n直达火车 {len(d['trains'])} 条，机票 {len(d['flights'])} 条（{d['flight_source']}）")
    for t in d["trains"][:5]:
        print(f"  {t['code']:<6s} {t['from_station']}->{t['to_station']} {t['depart']}-{t['arrive']} "
              f"{t['duration']} {t['seat']} ¥{t['price']:.0f}")
    print(f"\n中转方案（请求数 {planner.client.requests}）：")
    for o in planner.transfer("北京", "上海", day, max_options=4):
        a, b = o["legs"]
        print(f"  经{o['hub']} {a['code']}({a['depart']}-{a['arrive']}) -> {b['code']}({b['depart']}-{b['arrive']}) "
              f"换乘{o['transfer_text']} 总价¥{o['price']:.0f} 总耗时{o['duration']}")
    if planner.errors:
        print("\n错误:", planner.errors[:3])
