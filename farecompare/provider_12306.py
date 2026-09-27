"""12306 火车票适配器（实时数据，含真实票价）。

真实接口（均为公开查询接口，低频使用）：
  1) 会话预热   GET  /otn/leftTicket/init
  2) 余票查询   GET  /otn/leftTicket/query?leftTicketDTO.train_date=&from_station=&to_station=&purpose_codes=ADULT
  3) 票价查询   GET  /otn/leftTicket/queryTicketPrice?train_no=&from_station_no=&to_station_no=&seat_types=&train_date=

关键点（实测反解）：
  * leftTicket 行以 "|" 分隔，下标：
      2=车次编码(train_no 查询票价用) 3=车次号 4=出发电报码 5=到达电报码
      6=出发站序号 7=到达站序号 8=发车 9=到达 10=历时
      16/17=from_station_no / to_station_no（查询票价用）
      32/33=各席别余票编码串  34/35=席别类型编码串(seat_types)
  * queryTicketPrice 返回键：
      "¥" 结尾价格为 A<码>；纯数字为余票数；"OT" 为特殊席别列表
      编码 9/A9=商务座, M=一等座, O=二等座(动车/高铁), WZ=无座,
           1/A1=硬座, 3/A3=硬卧, 4/A4=软卧, I/AI=一等卧, J/AJ=二等卧
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor

BASE = "https://kyfw.12306.cn"
INIT_URL = BASE + "/otn/leftTicket/init?linktypeid=dc"
QUERY_URL = BASE + "/otn/leftTicket/query"
PRICE_URL = BASE + "/otn/leftTicket/queryTicketPrice"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": INIT_URL,
}

# 席别编码 -> (中文名, 档位优先级, 舒适度)
SEAT_MAP: dict[str, tuple[str, int]] = {
    "A9": ("商务座", 100),
    "9": ("商务座", 100),
    "P": ("特等座", 95),
    "M": ("一等座", 80),
    "D": ("优选一等座", 85),
    "O": ("二等座", 60),
    "S": ("商务座", 100),
    "A6": ("高级软卧", 90),
    "6": ("高级软卧", 90),
    "A4": ("软卧", 75),
    "4": ("软卧", 75),
    "AI": ("一等卧", 70),
    "I": ("一等卧", 70),
    "AJ": ("二等卧", 55),
    "J": ("二等卧", 55),
    "A3": ("硬卧", 50),
    "3": ("硬卧", 50),
    "A2": ("软座", 45),
    "2": ("软座", 45),
    "A1": ("硬座", 30),
    "1": ("硬座", 30),
    "WZ": ("无座", 5),
}

# 席别编码 -> 中文名（用于 seat_types 串里识别"本车有哪些席别"）
SEAT_LETTER_NAME = {
    "9": "商务座", "P": "特等座", "M": "一等座", "D": "优选一等座", "O": "二等座",
    "S": "商务座", "6": "高级软卧", "4": "软卧", "I": "一等卧", "J": "二等卧",
    "3": "硬卧", "2": "软座", "1": "硬座", "W": "无座",
}

# 余票标记串（下标 26..32）在当前接口版本中多为空或仅 "有/无"，字段顺序在不同车次上
# 表现不一致，因此不用于对外展示；票价接口的可用性字段与车次可订标记作为唯一依据。

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class BudgetExceeded(RuntimeError):
    """本次搜索的 12306 请求预算已用完（用于保证搜索速度）。"""


class Ticket12306:
    """带 cookie 会话与节流的 12306 客户端。"""

    def __init__(self, min_interval: float = 1.2, timeout: float = 25.0) -> None:
        self.timeout = timeout
        # 实测：短间隔连续请求会被源站拦截并返回 HTML 提示页，因此：
        #   * 串行 + 最小间隔 1.2s
        #   * 全局速率闸门：60 秒内最多 55 次（留出余量，避免触发限流）
        #   * 一旦命中限流则全局冷却（默认 25s），避免连续撞墙
        self.min_interval = min_interval
        self.window_seconds = 60.0
        self.window_limit = 55
        self._lock = threading.Lock()
        self._last = 0.0
        self._window: list[float] = []
        self._cooldown_until = 0.0
        self.cooldowns = 0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=_TLS),
        )
        self._warm = False
        self._warm_lock = threading.Lock()
        self.last_error: str | None = None
        self.last_placeholder = 0
        self.requests = 0
        self._name_of = None          # 由 planner 注入车站名回填函数
        # 单次搜索内的去重缓存：同一区间/同一车次只请求一次，避免枢纽笛卡尔积重复查询
        self._ticket_cache: dict[tuple[str, str, str], list[dict]] = {}
        self._price_cache: dict[tuple[str, str, str, str], list[dict]] = {}
        self.budget_limit = 0
        self.budget_used = 0
        self.soft_limit = 0
        self.budget_exceeded = False

    def set_name_resolver(self, fn) -> None:
        """注入 "电报码 -> 中文站名" 解析函数（接口返回的 map 只含部分车站）。"""
        self._name_of = fn

    # ---------- 基础请求 ----------
    def _throttle(self) -> None:
        """串行 + 最小间隔 + 60 秒滑动窗口配额，双重限速。"""
        while True:
            with self._lock:
                now = time.time()
                if self._cooldown_until > now:
                    wait = self._cooldown_until - now
                else:
                    # 清理过期窗口记录
                    cutoff = now - self.window_seconds
                    self._window = [t for t in self._window if t > cutoff]
                    gap_wait = self.min_interval - (now - self._last)
                    quota_wait = 0.0
                    if len(self._window) >= self.window_limit:
                        quota_wait = self._window[0] + self.window_seconds - now + 0.05
                    wait = max(gap_wait, quota_wait, 0.0)
                    if wait <= 0:
                        self._last = now
                        self._window.append(now)
                        return
                time.sleep(min(wait, 30.0))

    def _cooldown(self, seconds: float = 25.0) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.time() + seconds)
            self.cooldowns += 1

    # ---------- 单次搜索的查询缓存与请求预算 ----------
    def begin_budget(self, limit: int = 45) -> None:
        """开始一次搜索：清空查询缓存并设置本次请求预算（超出即停止扩展）。"""
        with self._lock:
            self._ticket_cache.clear()
            self._price_cache.clear()
            self.budget_limit = limit
            self.budget_used = 0
            self.soft_limit = 0

    def set_soft_limit(self, used_before: int, extra: int) -> None:
        """设置"软上限"：从本次搜索已用次数起，最多再花 extra 次（用于中转配额）。"""
        with self._lock:
            self.soft_limit = min(self.budget_limit, used_before + extra) if self.budget_limit \
                else used_before + extra

    def clear_soft_limit(self) -> None:
        with self._lock:
            self.soft_limit = 0

    def budget_left(self) -> int:
        return max(0, self.budget_limit - self.budget_used)

    def _spend(self) -> None:
        with self._lock:
            cap = min(self.budget_limit, self.soft_limit) if self.soft_limit else self.budget_limit
            if cap and self.budget_used >= cap:
                self.budget_exceeded = True
                raise BudgetExceeded(
                    f"已达本次搜索的 12306 请求上限（{cap} 次），为保证速度提前结束")
            self.budget_used += 1

    def _get(self, url: str) -> bytes:
        self._throttle()
        req = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(req, timeout=self.timeout) as r:
            self.requests += 1
            return r.read()


    @staticmethod
    def _loads(raw: bytes) -> dict:
        """12306 部分响应带 UTF-8 BOM；被限流时返回 HTML 提示页。"""
        if raw[:1] not in (b"{", b"\xef", b"\x00"):
            raise RuntimeError("源站返回非 JSON（可能触发限流）")
        text = raw.decode("utf-8-sig", "replace").strip()
        if not text.startswith("{"):
            raise RuntimeError("源站返回非 JSON（可能触发限流）")
        return json.loads(text)

    def _get_json(self, url: str, attempts: int = 3) -> dict:
        """带冷却退避的 JSON 请求；命中限流则全局冷却后再试。"""
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return self._loads(self._get(url))
            except Exception as exc:
                last_exc = exc
                self.last_error = str(exc)
                if "非 JSON" in str(exc):
                    self._cooldown(20.0 + 10.0 * i)   # 被限流：冷却 20/30/40 秒
                else:
                    time.sleep(1.0 + 1.5 * i)
        raise RuntimeError(str(last_exc))


    def warmup(self) -> None:
        with self._warm_lock:
            if self._warm:
                return
            try:
                self._get(INIT_URL)
                self._warm = True
            except Exception as exc:  # 预热失败仍可尝试查询
                self.last_error = f"会话预热失败: {exc}"

    # ---------- 余票 ----------
    def query_tickets(self, from_code: str, to_code: str, date: str,
                      strict: bool = True) -> list[dict]:
        """查询某日某区间车次。

        strict=True 时剔除 12306 在"该区间无直达车"时返回的占位行：
          * 到发站电报码与所查区间不一致的行
          * 发车/到达为 24:00 、历时为 99:59 的占位值
          * booked 标记为 IS_TIME_NOT_BUY（未到起售时间）等占位状态

        同一搜索内相同区间只请求一次（结果缓存），显著降低请求数。
        """
        self.warmup()
        ckey = (from_code, to_code, date, strict)
        with self._lock:
            if ckey in self._ticket_cache:
                return self._ticket_cache[ckey]
        self._spend()
        qs = urllib.parse.urlencode({
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        })
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                payload = self._get_json(f"{QUERY_URL}?{qs}")
                data = payload.get("data") or {}
                rows = data.get("result") or []
                station_map = data.get("map") or {}
                out = []
                placeholders = 0
                self.last_placeholder = 0
                for row in rows:
                    rec = self._parse_row(row, station_map, date, self._name_of)
                    if not rec:
                        continue
                    if strict and not self._is_valid(rec, from_code, to_code):
                        if self._is_placeholder(rec, from_code, to_code):
                            placeholders += 1
                        continue
                    out.append(rec)
                self.last_placeholder = placeholders
                self.last_error = None
                with self._lock:
                    self._ticket_cache[ckey] = out
                return out
            except Exception as exc:
                last_exc = exc
                self.last_error = f"余票查询失败: {exc}"
                time.sleep(0.8 * (attempt + 1))
        if last_exc:
            raise RuntimeError(f"12306 余票查询失败: {last_exc}")
        return []

    @staticmethod
    def _is_placeholder(rec: dict, from_code: str, to_code: str) -> bool:
        """占位行：12306 用 24:00 / 99:59 / IS_TIME_NOT_BUY 表示"该区间未起售/无票可售"。"""
        if rec["from_code"] != from_code or rec["to_code"] != to_code:
            return False
        state = (rec.get("book_state") or "").upper()
        return (rec["depart"] == "24:00" or rec["duration"] == "99:59"
                or "NOT_BUY" in state or "IS_TIME" in state)

    @staticmethod
    def _is_valid(rec: dict, from_code: str, to_code: str) -> bool:
        if rec["from_code"] != from_code or rec["to_code"] != to_code:
            return False
        if rec["depart"] in ("24:00", "") or rec["arrive"] in ("24:00", ""):
            return False
        if rec["duration"] in ("99:59", ""):
            return False
        state = (rec.get("book_state") or "").upper()
        if "IS_TIME_NOT_BUY" in state or "NOT_BUY" in state:
            return False
        if rec["duration_min"] <= 0 or rec["duration_min"] > 48 * 60:
            return False
        return True



    @staticmethod
    def _parse_row(row: str, station_map: dict, date: str, name_of=None) -> dict | None:
        p = row.split("|")
        if len(p) < 36:
            return None
        def g(i: int, default: str = "") -> str:
            return p[i] if i < len(p) else default

        def sname(code: str) -> str:
            if code in station_map:
                return station_map[code]
            if name_of:                      # 回填：接口 map 只含部分车站
                return name_of(code)
            return code

        code = g(3)
        from_code, to_code = g(4), g(5)
        seat_types = g(35) or g(34)

        types: list[str] = []
        for c in seat_types:
            nm = SEAT_LETTER_NAME.get(c)
            if nm and nm not in types:
                types.append(nm)
        return {
            "train_no": g(2),
            "code": code,
            "from_code": from_code,
            "to_code": to_code,
            "from_no": g(16),
            "to_no": g(17),
            "from_station": sname(from_code),
            "to_station": sname(to_code),
            "depart": g(8),
            "arrive": g(9),
            "duration": g(10),
            "date": date,
            "duration_min": normalize_duration(g(10)),
            "seat_types": seat_types,
            "offered": types,                     # 本车席别
            "bookable": g(11) == "Y",
            "book_state": g(11),
            "secret": g(0),
        }


    # ---------- 票价 ----------
    def query_price(self, rec: dict) -> list[dict]:
        """查一个车次的票价，返回 [{seat, price, seat_rank, stand, key}]。同一车次只请求一次。"""
        self.warmup()
        pkey = (rec["train_no"], rec["from_no"], rec["to_no"], rec["date"])
        with self._lock:
            if pkey in self._price_cache:
                return self._price_cache[pkey]
        try:
            self._spend()
        except BudgetExceeded as exc:
            self.last_error = str(exc)
            return []
        qs = urllib.parse.urlencode({
            "train_no": rec["train_no"],
            "from_station_no": rec["from_no"],
            "to_station_no": rec["to_no"],
            "seat_types": rec["seat_types"],
            "train_date": rec["date"],
        })
        try:
            payload = self._get_json(f"{PRICE_URL}?{qs}")
        except Exception as exc:
            self.last_error = f"票价查询失败({rec['code']}): {exc}"
            return []

        data = payload.get("data")
        if not isinstance(data, dict):
            return []

        out: list[dict] = []
        seen: set[str] = set()

        for key, val in data.items():
            if key in ("train_no", "OT", "MIN"):
                continue
            if not isinstance(val, str) or "¥" not in val:
                continue
            name, rank = SEAT_MAP.get(key, (None, 0))
            if name is None:
                name = SEAT_LETTER_NAME.get(key.lstrip("A"), key)
            price = _to_float(val)
            if price is None or name in seen:
                continue
            seen.add(name)
            out.append({
                "seat": name,
                "price": price,
                "seat_rank": rank,
                "stand": name == "无座",
                "key": key,
            })

        # 特殊席别（如一/二等卧），来自 OT 列表
        ot = data.get("OT")
        if isinstance(ot, list):
            for entry in ot:
                if not isinstance(entry, str) or ":" not in entry:
                    continue
                name, _, pv = entry.partition(":")
                name = name.strip()
                price = _to_float(pv)
                if price is None or name in seen:
                    continue
                seen.add(name)
                out.append({
                    "seat": name, "price": price,
                    "seat_rank": 65, "stand": False, "key": "OT",
                })

        out.sort(key=lambda x: x["price"])
        with self._lock:
            self._price_cache[pkey] = out
        return out

    def query_price_many(self, records: list[dict], limit: int = 8, workers: int = 1) -> list[dict]:
        """查询多个车次票价（按车次号去重）。默认串行——源站限流，串行最稳。"""
        unique: list[dict] = []
        seen: set[str] = set()
        for r in records:
            key = f"{r['train_no']}|{r['from_no']}|{r['to_no']}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        targets = unique[:limit]
        if not targets:
            return []
        if workers <= 1:
            results = [self.query_price(r) for r in targets]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self.query_price, targets))
        for rec, prices in zip(targets, results):
            rec["prices"] = prices
        return targets


def _to_float(text: str) -> float | None:
    try:
        return float(str(text).replace("¥", "").replace("￥", "").replace(",", "").strip())
    except Exception:
        return None


def normalize_duration(text: str) -> int:
    """'05:56' -> 356 分钟。"""
    try:
        h, m = text.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def duration_text(minutes: int) -> str:
    if minutes <= 0:
        return "-"
    return f"{minutes // 60}小时{minutes % 60:02d}分"


# 主要车站优先级：同一城市优先用这些站组合查询，减少无效请求
MAJOR_STATIONS: dict[str, list[str]] = {
    "北京": ["北京南", "北京", "北京西", "北京朝阳", "北京丰台"],
    "上海": ["上海虹桥", "上海", "上海南"],
    "广州": ["广州南", "广州", "广州东", "广州白云"],
    "深圳": ["深圳北", "深圳", "深圳东", "福田"],
    "天津": ["天津", "天津西", "天津南"],
    "重庆": ["重庆北", "重庆西", "重庆"],
    "成都": ["成都东", "成都", "成都南"],
    "杭州": ["杭州东", "杭州", "杭州西"],
    "南京": ["南京南", "南京"],
    "武汉": ["武汉", "汉口", "武昌"],
    "西安": ["西安北", "西安"],
    "郑州": ["郑州东", "郑州"],
    "济南": ["济南西", "济南", "济南东"],
    "长沙": ["长沙南", "长沙"],
    "合肥": ["合肥南", "合肥"],
    "福州": ["福州", "福州南"],
    "厦门": ["厦门北", "厦门"],
    "南昌": ["南昌西", "南昌"],
    "沈阳": ["沈阳北", "沈阳", "沈阳南"],
    "哈尔滨": ["哈尔滨西", "哈尔滨"],
    "长春": ["长春西", "长春"],
    "大连": ["大连北", "大连"],
    "青岛": ["青岛北", "青岛"],
    "石家庄": ["石家庄", "石家庄北"],
    "太原": ["太原南", "太原"],
    "南宁": ["南宁东", "南宁"],
    "贵阳": ["贵阳北", "贵阳"],
    "昆明": ["昆明南", "昆明"],
    "兰州": ["兰州西", "兰州"],
    "西宁": ["西宁"],
    "银川": ["银川"],
    "乌鲁木齐": ["乌鲁木齐"],
    "拉萨": ["拉萨"],
    "海口": ["海口东", "海口"],
    "三亚": ["三亚"],
    "苏州": ["苏州", "苏州北"],
    "无锡": ["无锡", "无锡东"],
    "徐州": ["徐州东", "徐州"],
    "常州": ["常州", "常州北"],
    "宁波": ["宁波"],
    "温州": ["温州南", "温州"],
}


def ordered_stations(city: str, stations, limit: int = 4) -> list[dict]:
    """按主要站优先级返回该城市的车站（名称, 电报码）。"""
    names = stations.stations_of_city(city)
    preferred = [n for n in MAJOR_STATIONS.get(city, []) if n in names]
    rest = [n for n in names if n not in preferred]
    ordered = preferred + rest
    out: list[dict] = []
    for n in ordered:
        e = stations.by_name.get(n)
        if e:
            out.append({"name": n, "code": e["code"]})
        if len(out) >= limit:
            break
    if not out:
        code = stations.code_of(city)
        if code:
            out.append({"name": stations.name_of_code(code), "code": code})
    return out



if __name__ == "__main__":
    import sys
    import datetime
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .stations import get_stations

    st = get_stations()
    client = Ticket12306()
    day = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    recs = client.query_tickets(st.code_of("北京南"), st.code_of("上海虹桥"), day)
    print(f"{day} 北京南->上海虹桥 共 {len(recs)} 车次")
    priced = client.query_price_many(recs, limit=4)
    for r in priced:
        print(f"\n{r['code']} {r['from_station']}->{r['to_station']} {r['depart']}-{r['arrive']} "
              f"{r['duration']} 席别={r['offered']}")
        for p in r["prices"]:
            print(f"    {p['seat']:<8s} ¥{p['price']:<9.1f} 档位={p['seat_rank']}")
