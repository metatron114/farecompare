"""同程旅行（www.ly.com）机票适配器 —— 真实航班 + 真实票价。

为什么用同程：
  * 去哪儿网页版机票服务已下线（页面明示"2026-10-10 起停止提供"，引导使用 App）
  * 携程 flights.ctrip.com / m.ctrip.com 返回 432 whaleguard block（反爬）
  * 同程机票列表页可正常渲染，且**无需登录**即可获取：航班号、航司、机型、起降时刻、
    起降机场与航站楼、飞行时长、餐食、经济舱折后价与折扣。经实测验签。

本模块通过本机 Edge（CDP）渲染页面后取回结构化数据，**全程本地计算，不消耗 AI token**。

票价口径（对应"只取裸票价"要求）：
  * 取列表页"¥xxx 起 / x.x折经济舱"，这是**裸票价**（不含保险、接送机、加速包等可选加价项）
  * 不含中介搭售：本适配器从不点击"保险/套餐"，只读列表页明码标价
  * 机建燃油属强制税费，界面单独提示，不计入票价排序基准
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from concurrent.futures import Future

CITY_URL = "https://www.ly.com/flights/itinerary/oneway/{dep}-{arr}?date={date}"

# 无免费托运行李的航司（对应"屏蔽无托运航班"要求）
NO_FREE_BAGGAGE_AIRLINES = {
    "春秋航空", "西部航空", "九元航空", "中国联合航空", "联航", "祥鹏航空",
    "奥凯航空", "乌鲁木齐航空", "北部湾航空", "江西航空", "龙江航空",
    "幸福航空", "多彩贵州航空", "福州航空", "桂林航空", "金鹏航空",
    "天骄航空", "瑞丽航空", "青岛航空", "东海航空",
}

_AIRLINE_CODE = {
    "CA": "中国国航", "MU": "东方航空", "CZ": "南方航空", "HU": "海南航空",
    "ZH": "深圳航空", "MF": "厦门航空", "3U": "四川航空", "SC": "山东航空",
    "HO": "吉祥航空", "9C": "春秋航空", "GS": "天津航空", "KN": "中国联合航空",
    "FM": "上海航空", "JD": "首都航空", "PN": "西部航空", "AQ": "九元航空",
    "EU": "成都航空", "GJ": "长龙航空", "NS": "河北航空", "RY": "江西航空",
    "KY": "昆明航空", "QW": "青岛航空", "TV": "西藏航空", "UQ": "乌鲁木齐航空",
    "DR": "瑞丽航空", "GT": "桂林航空", "GX": "北部湾航空", "HX": "香港航空",
    "8L": "祥鹏航空", "BK": "奥凯航空", "Y8": "金鹏航空", "FU": "福州航空",
    "G5": "华夏航空", "JR": "幸福航空", "LT": "龙江航空", "DZ": "东海航空",
    "CN": "大新华航空", "UQ": "乌鲁木齐航空", "KY": "昆明航空", "TV": "西藏航空",
    "PN": "西部航空", "AQ": "九元航空", "EU": "成都航空", "GJ": "长龙航空",
    "NS": "河北航空", "QW": "青岛航空", "DR": "瑞丽航空", "GT": "桂林航空",
    "GX": "北部湾航空", "HX": "香港航空", "NX": "澳门航空", "CI": "中华航空",
    "BR": "长荣航空", "SQ": "新加坡航空", "CX": "国泰航空", "KE": "大韩航空",
    "NH": "全日空", "JL": "日本航空", "TG": "泰国国际航空", "MH": "马来西亚航空",
    "AK": "亚洲航空", "VJ": "越捷航空", "VN": "越南航空", "PR": "菲律宾航空",
    "SU": "俄罗斯航空", "EK": "阿联酋航空", "QR": "卡塔尔航空", "ET": "埃塞俄比亚航空",
    "AI": "印度航空", "UL": "斯里兰卡航空", "RA": "尼泊尔航空", "OM": "蒙古航空",
    "K6": "柬埔寨吴哥航空", "ZA": "澜湄航空", "LQ": "澜湄航空", "KR": "柬埔寨航空",
    "RS": "首尔航空", "LJ": "真航空", "TW": "德威航空", "7C": "济州航空",
    "BX": "釜山航空", "ZE": "易斯达航空", "MM": "乐桃航空", "GK": "捷星日本",
    "IJ": "春秋航空日本", "9C": "春秋航空",
}

# ---------------------------------------------------------------------------
# 页面解析：在浏览器里执行，返回 JSON
# ---------------------------------------------------------------------------
EXTRACT_JS = r"""
(() => {
  const out = [];
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('.flight-item').forEach(el => {
    const raw = norm(el.innerText);
    if (!raw) return;
    const m = raw.match(/([A-Z]{2}\d{3,4})/);
    if (!m) return;
    const code = m[1];
    const times = raw.match(/\b\d{1,2}:\d{2}\b/g) || [];
    const priceM = raw.match(/[¥￥]\s?(\d{2,6})/);
    const discM = raw.match(/([\d.]+)\s*折/);
    const durM = raw.match(/(\d+)h\s*(\d+)m/) || raw.match(/(\d+)\s*小时\s*(\d+)\s*分/);
    // 机场（可能带航站楼）
    const airports = (raw.match(/[\u4e00-\u9fa5A-Za-z0-9]{2,12}?(?:机场|航站楼)T?\d?/g) || []).map(norm);
    const stop = (raw.match(/(\d+)\s*经停/) || [])[1] || (raw.match(/经停/) ? '1' : '0');
    out.push({
      text: raw,
      flight_no: code,
      airline_code: code.slice(0, 2),
      depart: times[0] || '',
      arrive: times[1] || '',
      next_day: /\+1\s*天|\+1天|次日/.test(raw),
      duration_min: durM ? (parseInt(durM[1]) * 60 + parseInt(durM[2])) : 0,
      price: priceM ? parseFloat(priceM[1]) : null,
      discount: discM ? parseFloat(discM[1]) : null,
      airports: airports,
      cabin: (raw.match(/[\u4e00-\u9fa5]{0,4}经济舱/) || ['经济舱'])[0],
      meal: /有早餐|有正餐|有餐食|有小吃/.test(raw),
      stops: parseInt(stop) || 0,
      bag_text: (raw.match(/[^|]{0,12}(?:托运|行李)[^|]{0,20}/g) || []).join(' ').trim(),
    });
  });

  // 价格日历（同程在列表页直接给出未来一个月每日最低价）
  const cal = [];
  const body = document.body ? document.body.innerText : '';
  const calSrc = document.querySelector('[class*=calendar],[class*=price-cal],[class*=lowprice]');
  const calText = calSrc ? calSrc.innerText : body;
  for (const mm of calText.matchAll(/(\d{2})-(\d{2})\s*周[一二三四五六日]\s*[¥￥]\s?(\d{2,6})/g)) {
    const year = new Date().getFullYear();
    cal.push({mmdd: mm[1] + '-' + mm[2], price: parseFloat(mm[3])});
  }
  return {flights: out, calendar: cal, bodyLen: body.length,
          title: document.title, href: location.href};
})()
"""


# ---------------------------------------------------------------------------
# 浏览器常驻工作线程
# ---------------------------------------------------------------------------
class _BrowserWorker:
    """单线程持有 Edge 实例，串行处理抓取任务；浏览器失效时自动重启。

    注意：外部杀掉 Edge 进程后，常驻句柄会失效，因此每个任务失败时会重建浏览器并重试一次。
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.restarts = 0

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="ly-browser", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        from .browser import Browser  # 延迟导入，避免无 Edge 环境导入即失败
        browser = None
        while True:
            try:
                job = self._q.get()
            except Exception:
                break
            if job is None:
                break
            fut, fn = job
            if not fut.set_running_or_notify_cancel():
                continue
            last_exc: Exception | None = None
            for attempt in (1, 2):
                try:
                    if browser is None:
                        browser = Browser(headless=True, timeout=60)
                        self.restarts += 1
                    fut.set_result(fn(browser))
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    # 浏览器可能已失效，丢弃并在下一轮重建
                    try:
                        if browser:
                            browser.close()
                    except Exception:
                        pass
                    browser = None
                    if attempt == 1:
                        time.sleep(1.0)
            if last_exc is not None:
                fut.set_exception(RuntimeError(f"浏览器抓取失败: {type(last_exc).__name__}: "
                                               f"{str(last_exc)[:120]}"))
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    def submit(self, fn, timeout: float = 180.0):
        self._ensure()
        fut: Future = Future()
        self._q.put((fut, fn))
        return fut.result(timeout=timeout)


_worker = _BrowserWorker()


def _render(browser, url: str, wait_flight: bool = True, timeout: float = 45.0) -> dict:
    sid = browser.new_page()
    try:
        browser.navigate(sid, url, wait=3)
        if wait_flight:
            browser.wait_for(
                sid, "/\\b[A-Z]{2}\\d{3,4}\\b/.test(document.body.innerText)", timeout=timeout)
            time.sleep(2.5)          # 等价格与折扣渲染完成
        return browser.eval(sid, EXTRACT_JS) or {}
    finally:
        try:
            browser.send("Target.closeTarget", {"targetId": browser._sessions.pop(sid, "")})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def search_flights(dep_city: str, arr_city: str, date: str,
                   exclude_no_baggage: bool = True,
                   limit: int = 40) -> dict:
    """返回 {"flights": [...], "calendar": [...], "source": "ly", "error": str|None}"""
    dep = _city_code(dep_city)
    arr = _city_code(arr_city)
    if not dep or not arr:
        return {"flights": [], "calendar": [], "source": "ly",
                "error": f"未找到城市代码：{dep_city if not dep else arr_city}"}
    url = CITY_URL.format(dep=dep, arr=arr, date=date)
    try:
        data = _worker.submit(lambda b: _render(b, url))
    except Exception as exc:
        return {"flights": [], "calendar": [], "source": "ly",
                "error": f"渲染失败: {type(exc).__name__}: {str(exc)[:160]}"}

    flights = []
    for raw in data.get("flights", []):
        f = _normalize(raw)
        if not f:
            continue
        flights.append(f)

    # 去重（同航班多舱位只保留最低价）
    best: dict[str, dict] = {}
    for f in flights:
        k = f["flight_no"]
        if k not in best or (f["price"] or 1e9) < (best[k]["price"] or 1e9):
            best[k] = f
    flights = sorted(best.values(), key=lambda x: (x["price"] is None, x["price"]))

    filtered_out = []
    if exclude_no_baggage:
        keep = []
        for f in flights:
            if f["no_free_baggage"]:
                filtered_out.append(f)
            else:
                keep.append(f)
        flights = keep

    calendar = []
    for c in data.get("calendar", []):
        calendar.append(c)
    calendar.sort(key=lambda x: x["mmdd"])

    return {
        "flights": flights[:limit],
        "calendar": calendar,
        "source": "ly",
        "provider": "同程旅行",
        "url": url,
        "excluded_no_baggage": [{"flight_no": f["flight_no"], "airline": f["airline"],
                                 "price": f["price"]} for f in filtered_out],
        "error": None if flights else ("该航线当日无可售航班（或已全部被无托运规则过滤）"),
    }


def _city_code(city: str) -> str | None:
    from .airports import AIRPORTS, BY_IATA
    c = (city or "").strip()
    if not c:
        return None
    if c in AIRPORTS:
        return AIRPORTS[c].get("city_code")
    up = c.upper()
    if len(up) == 3 and up in BY_IATA:
        return AIRPORTS[BY_IATA[up]].get("city_code", up)
    # 城市名去掉"市"，或匹配内置别名
    c2 = c.rstrip("市")
    if c2 in AIRPORTS:
        return AIRPORTS[c2].get("city_code")
    # 用同程城市库
    try:
        from .ly_cities import city_code_of
        return city_code_of(c)
    except Exception:
        return None


def _normalize(raw: dict) -> dict | None:
    code = (raw.get("flight_no") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2}\d{3,4}", code):
        return None
    # 航司名以航班号字母前缀为准（页面文本里可能混入"华夏航空"这类销售方字样）
    prefix = "".join(ch for ch in code if ch.isalpha())
    airline = _AIRLINE_CODE.get(prefix, "")
    if not airline:
        m = re.search(r"(中国联合航空|中国国际航空|上海航空|[\u4e00-\u9fa5]{2,4}航空)",
                      raw.get("text", ""))
        airline = m.group(1) if m else prefix
    price = raw.get("price")
    no_bag = airline in NO_FREE_BAGGAGE_AIRLINES
    bag_text = raw.get("bag_text") or ""
    if re.search(r"无免费托运|无托运行李|不含托运行李|无行李额", bag_text):
        no_bag = True
    elif re.search(r"免费托运|含托运|有免费行李", bag_text):
        no_bag = False

    airports = raw.get("airports") or []
    return {
        "flight_no": code,
        "airline": airline,
        "airline_code": raw.get("airline_code", ""),
        "depart_time": raw.get("depart", ""),
        "arrive_time": raw.get("arrive", ""),
        "arrive_next_day": bool(raw.get("next_day")),
        "duration_min": raw.get("duration_min", 0),
        "duration": _dur_text(raw.get("duration_min", 0)),
        "price": price,
        "bare_fare": True,             # 列表价即裸票价（不含保险等可选加价）
        "discount": raw.get("discount"),
        "cabin": raw.get("cabin", "经济舱"),
        "meal": bool(raw.get("meal")),
        "stops": raw.get("stops", 0),
        "dep_airport": airports[0] if airports else "",
        "arr_airport": airports[1] if len(airports) > 1 else "",
        "no_free_baggage": no_bag,
        "bag_text": bag_text,
        "source": "ly",
        "provider": "同程旅行",
        "raw": raw.get("text", "")[:220],
    }


def _dur_text(minutes: int) -> str:
    if not minutes:
        return "-"
    return f"{minutes // 60}小时{minutes % 60:02d}分"


def price_calendar(dep_city: str, arr_city: str, date: str) -> dict:
    """取同程列表页附带的未来一个月最低价日历（用于日期区间搜索）。"""
    res = search_flights(dep_city, arr_city, date, exclude_no_baggage=False, limit=1)
    return {"calendar": res.get("calendar", []), "error": res.get("error")}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = sys.argv[1] if len(sys.argv) > 1 else "2026-10-12"
    t0 = time.time()
    res = search_flights("北京", "上海", d)
    print(f"同程机票 {d}  用时 {time.time()-t0:.1f}s  source={res['source']}")
    print("error:", res["error"])
    print(f"航班 {len(res['flights'])} 个，价格日历 {len(res['calendar'])} 天")
    for f in res["flights"][:12]:
        print(f"  {f['flight_no']:<8s} {f['airline']:<8s} {f['depart_time']}-{f['arrive_time']}"
              f"{'+1' if f['arrive_next_day'] else '  '} {f['duration']:>10s} "
              f"¥{f['price']:.0f}" + (f" {f['discount']}折" if f['discount'] else "")
              + f"  {f['dep_airport'][:8]}→{f['arr_airport'][:10]}"
              + ("  有餐" if f["meal"] else "")
              + ("  ⚠无托运" if f["no_free_baggage"] else ""))
    if res["excluded_no_baggage"]:
        print("被过滤（无免费托运）:", res["excluded_no_baggage"][:6])
    if res["calendar"]:
        print("价格日历样本:", res["calendar"][:10])
