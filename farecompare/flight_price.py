"""机票参考价模型（非实时报价，界面必须明确标注）。

为什么需要它：
  去哪儿与携程的机票实时接口均被反爬拦截（见 provider_flight.py 说明），
  在无法取得实时报价时，用公开的民航定价规律给出"参考价"，用于票价量级比较。
  任何排序结果都会带上 source="estimate" 标记，前端以醒目样式区分。

模型（确定性，可复现）：
  1) 航线距离 D = 城市间大圆距离 × 1.09（航路绕行系数）
  2) 基准全价： F = 220 + 0.92 × D × 舱位系数(经济舱=1.0)，并做长航线递减
  3) 折扣率由 (航线、日期、航班序号) 的稳定哈希决定，区间随提前天数变化：
       提前 <=3 天   [0.85, 1.20]
       提前 <=14 天  [0.55, 1.00]
       提前 <=30 天  [0.40, 0.85]
        提前 >30 天  [0.35, 0.75]
  4) 周五/周日溢价 +8%，周二/周三折扣 -6%
  5) 机建燃油附加费：机场建设费 50 元 + 燃油附加费（按距离 20/40/70 元）
"""

from __future__ import annotations

import datetime
import hashlib

from .airports import AIRPORTS, route_km

FUEL_SURCHARGE = ((800, 20), (1600, 40), (float("inf"), 70))
AIRPORT_FEE = 50.0

AIRLINES = [
    ("CA", "中国国航"), ("MU", "东方航空"), ("CZ", "南方航空"), ("HU", "海南航空"),
    ("ZH", "深圳航空"), ("MF", "厦门航空"), ("3U", "四川航空"), ("SC", "山东航空"),
    ("HO", "吉祥航空"), ("9C", "春秋航空"), ("GS", "天津航空"), ("KN", "中国联航"),
]
# 航班时刻表（出发小时:分钟），覆盖早中晚班
SLOTS = ["07:05", "08:20", "09:35", "11:10", "12:40", "14:05", "15:30", "17:15", "18:50", "20:25", "21:40"]


def _stable_hash(*parts: str) -> float:
    """返回 [0,1) 的稳定伪随机数，保证同一输入结果一致。"""
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return int(digest[:12], 16) / float(0x1000000000000)


def _discount_range(days_ahead: int) -> tuple[float, float]:
    if days_ahead <= 3:
        return 0.85, 1.20
    if days_ahead <= 14:
        return 0.55, 1.00
    if days_ahead <= 30:
        return 0.40, 0.85
    return 0.35, 0.75


def _base_full_fare(distance_km: float) -> float:
    """全价经济舱基准（元）。距离越长单公里价越低。"""
    if distance_km <= 500:
        unit = 1.05
    elif distance_km <= 1000:
        unit = 0.92
    elif distance_km <= 1800:
        unit = 0.80
    else:
        unit = 0.70
    return 220.0 + unit * distance_km


def _flight_minutes(distance_km: float) -> int:
    """巡航时间 + 起降滑行时间。"""
    cruise = distance_km / 12.5          # 约 750km/h
    return int(round(cruise + 45))


def _add_minutes(hhmm: str, minutes: int) -> tuple[str, int]:
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}", total // 60


def estimate_flights(dep_city: str, arr_city: str, date: str, limit: int = 8) -> list[dict]:
    """生成参考价机票列表（确定性）。"""
    if dep_city not in AIRPORTS or arr_city not in AIRPORTS:
        return []
    km = route_km(dep_city, arr_city) or 0.0
    if km <= 0:
        return []
    distance = km * 1.09

    try:
        d = datetime.date.fromisoformat(date)
    except Exception:
        d = datetime.date.today() + datetime.timedelta(days=14)
    days_ahead = max(0, (d - datetime.date.today()).days)

    lo, hi = _discount_range(days_ahead)
    weekday = d.weekday()
    if weekday in (4, 6):        # 周五 / 周日
        lo, hi = lo * 1.08, hi * 1.08
    elif weekday in (1, 2):      # 周二 / 周三
        lo, hi = lo * 0.94, hi * 0.94

    full_fare = _base_full_fare(distance)
    dur = _flight_minutes(distance)
    fuel = next(v for th, v in FUEL_SURCHARGE if distance <= th)

    out: list[dict] = []
    for idx, slot in enumerate(SLOTS):
        air_code, air_name = AIRLINES[int(_stable_hash(dep_city, arr_city, str(idx)) * len(AIRLINES)) % len(AIRLINES)]
        flight_no = f"{air_code}{1000 + int(_stable_hash(dep_city, arr_city, slot, 'no') * 8000)}"
        r = _stable_hash(dep_city, arr_city, date, slot)
        discount = lo + (hi - lo) * r
        price = full_fare * discount

        # 早班/晚班折扣更深（红眼/首班）
        if slot <= "08:00" or slot >= "20:00":
            price *= 0.93
        price = max(price, 190.0)

        total = round(price + fuel + AIRPORT_FEE)
        arrive, arrive_day = _add_minutes(slot, dur)
        punctuality = 70 + int(_stable_hash(dep_city, arr_city, slot, "p") * 28)

        out.append({
            "flight_no": flight_no,
            "airline": air_name,
            "airline_code": air_code,
            "depart_time": slot,
            "arrive_time": arrive,
            "arrive_next_day": arrive_day > 23,
            "duration_min": dur,
            "duration": f"{dur // 60}小时{dur % 60:02d}分",
            "list_price": round(full_fare),
            "discount": round(discount, 2),
            "fare": round(price),
            "fuel_fee": fuel,
            "airport_fee": AIRPORT_FEE,
            "price": float(total),
            "seats_left": 1 + int(_stable_hash(flight_no, date, "s") * 9),
            "on_time_rate": punctuality,
            "dep_airport": AIRPORTS[dep_city]["airport"],
            "arr_airport": AIRPORTS[arr_city]["airport"],
            "dep_iata": AIRPORTS[dep_city]["iata"],
            "arr_iata": AIRPORTS[arr_city]["iata"],
        })

    out.sort(key=lambda x: x["price"])
    return out[:limit]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for dep, arr in (("北京", "上海"), ("上海", "成都"), ("广州", "乌鲁木齐")):
        day = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
        rows = estimate_flights(dep, arr, day, limit=4)
        print(f"\n{dep} -> {arr}  {day}  距离 {route_km(dep, arr):.0f}km")
        for f in rows:
            print(f"  {f['flight_no']:<8s} {f['airline']:<6s} {f['depart_time']}-{f['arrive_time']} "
                  f"{f['duration']:<10s} 全价¥{f['list_price']:<6d} {f['discount']:.2f}折 "
                  f"合计¥{f['price']:.0f}")
