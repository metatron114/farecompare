"""机票数据源调度：多源实时抓取，按可靠性依次降级。

数据源优先级（均为实测结论）：
  1) 同程旅行 www.ly.com        —— ✅ 可用。真实航班号/航司/机型/时刻/机场/餐食/裸票价/折扣，
                                  无需登录；列表页还附带未来一个月最低价日历。
  2) 携程     www.ctrip.com     —— ⚠️ flights.ctrip.com 与 m.ctrip.com 返回 432 whaleguard block
  3) 去哪儿   flight.qunar.com  —— ⚠️ 网页版机票服务已下线（页面明示停止提供），接口需前端签名
  4) 内置参考价模型              —— 前三个源都取不到时兜底，界面明确标注"参考价"

票价口径（"只取裸票价，剔除中介加价"）：
  * 只读列表页明码标价的机票价格，**从不点击保险/套餐/加速包**
  * 机建燃油属强制税费，单独提示，不计入比价基准
  * 无免费托运行李的航司可一键屏蔽（默认屏蔽）

全部解析在本机完成，不调用任何外部 AI 服务，**不消耗 AI token**。
"""

from __future__ import annotations

import datetime
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .airports import AIRPORTS
from .flight_price import estimate_flights

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

PROVIDER_LABEL = {
    "ly": "同程旅行",
    "ctrip": "携程",
    "qunar": "去哪儿",
    "estimate": "参考价模型",
}


# ---------------------------------------------------------------------------
# 官方查询深链
# ---------------------------------------------------------------------------
def qunar_url(dep_city: str, arr_city: str, date: str) -> str:
    return "https://flight.qunar.com/site/oneway_list.htm?" + urllib.parse.urlencode({
        "fromCity": dep_city, "toCity": arr_city, "fromDate": date, "startDate": date,
    })


def ctrip_url(dep_city: str, arr_city: str, date: str) -> str:
    dep, arr = AIRPORTS.get(dep_city, {}), AIRPORTS.get(arr_city, {})
    d, a = dep.get("city_code", ""), arr.get("city_code", "")
    if d and a:
        return f"https://flights.ctrip.com/online/list/oneway-{d.lower()}-{a.lower()}?depdate={date}"
    return "https://flights.ctrip.com/online/channel/domestic"


def ly_url(dep_city: str, arr_city: str, date: str) -> str:
    from .ly_cities import city_code_of
    d, a = city_code_of(dep_city), city_code_of(arr_city)
    if d and a:
        return f"https://www.ly.com/flights/itinerary/oneway/{d}-{a}?date={date}"
    return "https://www.ly.com/flights"


def deep_links(dep_city: str, arr_city: str, date: str) -> dict:
    return {
        "ly": ly_url(dep_city, arr_city, date),
        "ctrip": ctrip_url(dep_city, arr_city, date),
        "qunar": qunar_url(dep_city, arr_city, date),
    }


# ---------------------------------------------------------------------------
# 源 1：同程旅行（真实数据）
# ---------------------------------------------------------------------------
def _from_ly(dep_city: str, arr_city: str, date: str,
             exclude_no_baggage: bool = True) -> dict | None:
    try:
        from .provider_ly import search_flights as ly_search
    except Exception:
        return None
    res = ly_search(dep_city, arr_city, date, exclude_no_baggage=exclude_no_baggage)
    flights = res.get("flights") or []
    if not flights:
        return None
    out = []
    for f in flights:
        if not f.get("price"):
            continue
        out.append({
            "flight_no": f["flight_no"],
            "airline": f["airline"],
            "airline_code": f.get("airline_code", ""),
            "depart_time": f["depart_time"],
            "arrive_time": f["arrive_time"],
            "arrive_next_day": f.get("arrive_next_day", False),
            "duration_min": f.get("duration_min", 0),
            "duration": f.get("duration", "-"),
            "price": float(f["price"]),
            "bare_fare": True,
            "discount": f.get("discount"),
            "cabin": f.get("cabin", "经济舱"),
            "meal": f.get("meal", False),
            "stops": f.get("stops", 0),
            "dep_airport": f.get("dep_airport", ""),
            "arr_airport": f.get("arr_airport", ""),
            "no_free_baggage": f.get("no_free_baggage", False),
            "source": "ly",
            "provider": "同程旅行",
        })
    return {
        "flights": out,
        "calendar": res.get("calendar") or [],
        "source": "ly",
        "provider": "同程旅行",
        "url": res.get("url", ""),
        "excluded_no_baggage": res.get("excluded_no_baggage") or [],
        "note": "同程旅行实时报价（裸票价，不含保险等可选加价项）",
    }


# ---------------------------------------------------------------------------
# 源 2 / 3：携程、去哪儿（保留实现，可达时自动生效）
# ---------------------------------------------------------------------------
def _from_ctrip(dep_city: str, arr_city: str, date: str) -> dict | None:
    dep, arr = AIRPORTS.get(dep_city, {}), AIRPORTS.get(arr_city, {})
    if not dep.get("city_code") or not arr.get("city_code"):
        return None
    body = json.dumps({
        "searchType": "D", "departureCity": dep["city_code"],
        "arrivalCity": arr["city_code"], "departureDate": date,
    }).encode()
    req = urllib.request.Request(
        "https://m.ctrip.com/restapi/soa2/14022/FlightListSearch", data=body,
        headers={**UA, "Content-Type": "application/json;charset=UTF-8",
                 "Referer": "https://m.ctrip.com/html5/flight/"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_TLS) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    items = obj.get("fltitem") or []
    if not items:
        return None
    out = []
    for it in items:
        price = None
        for k in ("price", "adultPrice", "lowestPrice", "cabinPrice"):
            if it.get(k):
                price = float(it[k])
                break
        if not price:
            continue
        out.append({
            "flight_no": it.get("flightNo") or it.get("flightNumber") or "",
            "airline": it.get("airlineName") or "",
            "depart_time": (it.get("departTime") or "")[-5:],
            "arrive_time": (it.get("arriveTime") or "")[-5:],
            "duration_min": 0, "duration": "-",
            "price": price, "bare_fare": True,
            "source": "ctrip", "provider": "携程",
            "no_free_baggage": False,
        })
    if not out:
        return None
    return {"flights": out, "calendar": [], "source": "ctrip", "provider": "携程",
            "note": "携程实时报价（裸票价）"}


def _from_qunar(dep_city: str, arr_city: str, date: str) -> dict | None:
    payload = {"departureCity": dep_city, "arrivalCity": arr_city,
               "departureDate": date, "ex_track": ""}
    req = urllib.request.Request(
        "https://flight.qunar.com/touch/api/domestic/wbdflightlist",
        data=urllib.parse.urlencode(payload).encode(),
        headers={**UA, "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://flight.qunar.com/"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_TLS) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    data = obj.get("data") or {}
    flights = data.get("flights") or []
    if obj.get("code") != 0 or not flights:
        return None
    out = []
    for f in flights:
        try:
            out.append({
                "flight_no": f.get("flightNumber") or f.get("code") or "",
                "airline": f.get("airlineName") or "",
                "depart_time": (f.get("depTime") or "")[-5:],
                "arrive_time": (f.get("arrTime") or "")[-5:],
                "duration_min": 0, "duration": "-",
                "price": float(f.get("price") or f.get("adultPrice") or 0),
                "bare_fare": True, "source": "qunar", "provider": "去哪儿",
                "no_free_baggage": False,
            })
        except Exception:
            continue
    if not out:
        return None
    return {"flights": out, "calendar": [], "source": "qunar", "provider": "去哪儿",
            "note": "去哪儿实时报价（裸票价）"}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
_SOURCE_ENTRY = {
    "auto": [("ly", _from_ly), ("ctrip", _from_ctrip), ("qunar", _from_qunar)],
    "ly": [("ly", _from_ly)],
    "ctrip": [("ctrip", _from_ctrip)],
    "qunar": [("qunar", _from_qunar)],
    "estimate": [],
}


def search_flights(dep_city: str, arr_city: str, date: str,
                   source: str = "auto",
                   exclude_no_baggage: bool = True,
                   limit: int = 30) -> dict:
    """查询机票。返回 {"flights": [...], "source": ..., "note": ..., "calendar": [...]}"""
    notes: list[str] = []
    for name, fn in _SOURCE_ENTRY.get(source, _SOURCE_ENTRY["auto"]):
        try:
            res = fn(dep_city, arr_city, date) if name != "ly" else fn(
                dep_city, arr_city, date, exclude_no_baggage=exclude_no_baggage)
        except Exception as exc:
            notes.append(f"{PROVIDER_LABEL.get(name, name)} 抓取异常：{str(exc)[:80]}")
            continue
        if res and res.get("flights"):
            rows = sorted(res["flights"], key=lambda x: x["price"])[:limit]
            for r in rows:
                r.setdefault("provider", res.get("provider", ""))
                r.setdefault("source", name)
            return {
                "flights": rows,
                "calendar": res.get("calendar") or [],
                "source": res.get("source", name),
                "provider": res.get("provider", PROVIDER_LABEL.get(name, name)),
                "url": res.get("url", ""),
                "excluded_no_baggage": res.get("excluded_no_baggage") or [],
                "note": res.get("note", ""),
                "live": True,
                "errors": notes,
            }
        if res is None:
            notes.append(f"{PROVIDER_LABEL.get(name, name)} 未取到数据")

    # 兜底：参考价模型
    rows = estimate_flights(dep_city, arr_city, date, limit=limit)
    for r in rows:
        r["source"] = "estimate"
        r["provider"] = "参考价模型"
        r["live"] = False
        r["no_free_baggage"] = r.get("airline") in {
            "春秋航空", "中国联合航", "西部航空", "九元航空"}
    if exclude_no_baggage:
        rows = [r for r in rows if not r.get("no_free_baggage")]
    return {
        "flights": rows, "calendar": [], "source": "estimate", "provider": "参考价模型",
        "live": False, "excluded_no_baggage": [], "errors": notes,
        "note": "三个实时机票源均未取到数据，以下为参考价（非实时），请点击“核验”到官方页面确认。",
    }


def search_range(dep_city: str, arr_city: str, start: str, end: str,
                 min_price: bool = True) -> dict:
    """日期区间搜索：用同程列表页自带的未来一个月价格日历，给出区间内每日最低票价。

    返回 {"days": [{date, weekday, price}], "cheapest": {...}, "source": "ly"}
    """
    try:
        d0 = datetime.date.fromisoformat(start)
        d1 = datetime.date.fromisoformat(end)
    except Exception:
        return {"days": [], "error": "日期格式应为 YYYY-MM-DD"}
    if d1 < d0:
        d0, d1 = d1, d0

    res = search_flights(dep_city, arr_city, start, source="ly",
                        exclude_no_baggage=False, limit=1)
    cal = res.get("calendar") or []
    if not cal:
        return {"days": [], "source": res.get("source"),
                "error": res.get("note") or "价格日历不可用"}

    year = d0.year
    days = []
    for item in cal:
        mmdd = item.get("mmdd") or ""
        try:
            mm, dd = mmdd.split("-")
            d = datetime.date(year, int(mm), int(dd))
            if d < d0 - datetime.timedelta(days=200):
                d = datetime.date(year + 1, int(mm), int(dd))
        except Exception:
            continue
        if d0 <= d <= d1:
            days.append({"date": d.isoformat(), "weekday": "周" + "一二三四五六日"[d.weekday()],
                         "price": item.get("price")})
    days.sort(key=lambda x: x["date"])
    cheapest = min((d for d in days if d.get("price")), key=lambda x: x["price"], default=None)
    return {"days": days, "cheapest": cheapest, "source": "ly",
            "provider": "同程旅行", "range": {"start": d0.isoformat(), "end": d1.isoformat()},
            "note": "同程旅行价格日历（裸票价，不含保险等可选加价项）"}


def provider_status() -> dict:
    """探测各机票源连通性。"""
    status: dict = {}
    probe = [
        ("ly", "https://www.ly.com/flights",
         "可渲染真实航班列表，无需登录（主数据源）"),
        ("ctrip", "https://www.ctrip.com/", "站点可达，但机票接口返回 432 whaleguard block"),
        ("qunar", "https://flight.qunar.com/",
         "站点可达，但网页版机票服务已下线，接口需前端签名"),
    ]
    for key, url, note in probe:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10, context=_TLS) as r:
                code = r.status
            status[key] = {"reachable": True, "http": code, "note": note,
                           "live_data": key == "ly"}
        except urllib.error.HTTPError as exc:
            status[key] = {"reachable": True, "http": exc.code, "note": note,
                           "live_data": key == "ly"}
        except Exception as exc:
            status[key] = {"reachable": False, "error": str(exc)[:100], "note": note,
                           "live_data": False}
    return status


def browser_backend() -> dict:
    """当前用于抓取机票的浏览器后端信息。

    说明：系统默认浏览器是火狐，但抓取使用 Edge/Chrome 的 DevTools 协议（CDP）：
    只需标准库、无窗口、不触碰火狐的登录态，因此不影响你日常用火狐上网。
    """
    import os
    from .browser import EDGE_CANDIDATES
    exe = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), None)
    name = "无"
    if exe:
        name = "Edge (CDP)" if "Edge" in exe else "Chrome (CDP)"
    return {
        "name": name,
        "path": exe or "",
        "headless": True,
        "note": "抓取走无头 Edge，不影响系统默认浏览器（火狐）的浏览与登录态",
        "system_default_browser": _default_browser(),
    }


def _default_browser() -> str:
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice")
        prog = winreg.QueryValueEx(k, "ProgId")[0]
        return "火狐 Firefox" if "Firefox" in prog else prog
    except Exception:
        return "未知"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    day = sys.argv[1] if len(sys.argv) > 1 else (
        datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    res = search_flights("北京", "上海", day)
    print(f"日期 {day} | 源 {res['source']}（{res['provider']}）| 实时 {res.get('live')}")
    print("说明:", res["note"])
    if res.get("excluded_no_baggage"):
        print("已屏蔽无托运:", [f"{x['flight_no']} {x['airline']}" for x in res["excluded_no_baggage"]][:6])
    for f in res["flights"][:10]:
        print(f"  {f['flight_no']:<8s} {f['airline']:<8s} {f['depart_time']}-{f['arrive_time']} "
              f"¥{f['price']:.0f}" + (f" {f['discount']}折" if f.get('discount') else ""))
    if res.get("errors"):
        print("降级记录:", res["errors"])
