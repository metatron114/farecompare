"""比价排序引擎：统一火车 / 飞机 / 中转方案的排序与筛选。

排序键（默认）：总价升序 → 总耗时升序 → 方案类型
排序维度：price(默认) / duration / depart / transfers
筛选：交通方式、席别/舱位档次、是否只看直达、预算上限、出发时段
"""

from __future__ import annotations

from .provider_12306 import duration_text

MODE_LABEL = {"train": "火车", "flight": "飞机", "transfer": "火车中转",
              "airrail": "航铁联程"}


def _time_key(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 10 ** 6


def _train_option(leg: dict, dep_city: str, arr_city: str, sources: dict) -> dict:
    live = leg.get("live", True)
    src_label = leg.get("source_label") or ("12306 实时" if live else "gaotie.com.cn")
    return {
        "kind": "train",
        "mode": "train",
        "mode_label": MODE_LABEL["train"],
        "title": f"{leg['code']} {leg['seat']}",
        "route": f"{leg['from_station']} → {leg['to_station']}",
        "depart": leg["depart"],
        "arrive": leg["arrive"],
        "duration_min": leg["duration_min"],
        "duration": leg["duration"],
        "price": leg["price"],
        "transfers": 0,
        "legs": [leg],
        "seat": leg["seat"],
        "all_seats": leg.get("all_seats", []),
        "source": leg.get("source", "12306"),
        "source_label": src_label,
        "live": live,
        "stops": leg.get("stops", 0),
        "bookable": leg.get("bookable", True),
        "url": "https://kyfw.12306.cn/otn/leftTicket/init",
        "note": ("公布票价（gaotie.com.cn），实时折扣以 12306 为准" if not live else ""),
    }


def _flight_option(f: dict, dep_city: str, arr_city: str, links: dict) -> dict:
    est = f.get("source") == "estimate" or f.get("live") is False
    provider = f.get("provider") or ("参考价模型" if est else "同程旅行")
    note_bits = []
    if f.get("stops"):
        note_bits.append(f"经停{f['stops']}次")
    if f.get("meal"):
        note_bits.append("含餐")
    if f.get("no_free_baggage"):
        note_bits.append("⚠ 无免费托运")
    return {
        "kind": "flight",
        "mode": "flight",
        "mode_label": MODE_LABEL["flight"],
        "title": f"{f.get('flight_no','')} {f.get('airline','')}",
        "route": f"{f.get('dep_airport') or dep_city} → {f.get('arr_airport') or arr_city}",
        "depart": f.get("depart_time", ""),
        "arrive": f.get("arrive_time", ""),
        "arrive_next_day": f.get("arrive_next_day", False),
        "duration_min": f.get("duration_min", 0),
        "duration": f.get("duration", "-"),
        "price": f.get("price", 0),
        "transfers": 0,
        "legs": [{
            "mode": "flight",
            "code": f.get("flight_no", ""),
            "from_station": f.get("dep_airport", dep_city),
            "to_station": f.get("arr_airport", arr_city),
            "depart": f.get("depart_time", ""),
            "arrive": f.get("arrive_time", ""),
            "duration": f.get("duration", "-"),
            "seat": f.get("cabin", "经济舱"),
            "price": f.get("price", 0),
        }],
        "seat": f.get("cabin", "经济舱"),
        "all_seats": [],
        "source": f.get("source", "estimate"),
        "source_label": ("参考价（非实时）" if est else f"{provider} 实时"),
        "provider": provider,
        "live": not est,
        "discount": f.get("discount"),
        "list_price": f.get("list_price"),
        "fees": f.get("fees"),
        "bare_fare": bool(f.get("bare_fare")),
        "note_bits": note_bits,
        "no_free_baggage": bool(f.get("no_free_baggage")),
        "bookable": True,
        "url": links.get("ly", "") if not est else links.get("qunar", ""),
        "urls": links,
        "note": "裸票价，不含保险等可选加价；机建燃油另计" if not est else "参考价，需到官方页面核验",
    }


def _transfer_option(o: dict, links: dict) -> dict:
    legs = o["legs"]
    return {
        "kind": "transfer",
        "mode": "train",
        "mode_label": MODE_LABEL["transfer"],
        "title": f"{legs[0]['code']} → {legs[1]['code']}（经{o['hub']}）",
        "route": o.get("route_text", ""),
        "depart": legs[0]["depart"],
        "arrive": legs[1]["arrive"],
        "duration_min": o["duration_min"],
        "duration": o["duration"],
        "price": o["price"],
        "transfers": 1,
        "legs": legs,
        "seat": f"{legs[0]['seat']} + {legs[1]['seat']}",
        "all_seats": [],
        "source": "12306",
        "source_label": "12306 实时",
        "live": True,
        "bookable": all(l.get("bookable", True) for l in legs),
        "transfer_min": o["transfer_min"],
        "transfer_text": o["transfer_text"],
        "overnight": bool(o.get("overnight")),
        "hub": o["hub"],
        "url": "https://kyfw.12306.cn/otn/leftTicket/init",
        "note": ("隔夜换乘：需在中转城市住宿一晚，次日早晨接续"
                 if o.get("overnight") else f"换乘缓冲 {o['transfer_text']}"),
    }


def _air_rail_option(o: dict, links: dict) -> dict:
    """航铁联程：飞机 + 高铁 两段组合。"""
    legs = o["legs"]
    air, rail = legs[0], legs[1]
    return {
        "kind": "airrail",
        "mode": "airrail",
        "mode_label": MODE_LABEL["airrail"],
        "title": f"{air['code']} + {rail['code']}（{o['hub']} 空地换乘）",
        "route": o.get("route_text", ""),
        "depart": air["depart"],
        "arrive": rail["arrive"],
        "duration_min": o["duration_min"],
        "duration": o["duration"],
        "price": o["price"],
        "transfers": 1,
        "legs": legs,
        "seat": f"{air['seat']} + {rail['seat']}",
        "all_seats": [],
        "source": "ly+12306",
        "source_label": "同程+12306 实时",
        "live": True,
        "bookable": True,
        "transfer_min": o["transfer_min"],
        "transfer_text": o["transfer_text"],
        "hub": o["hub"],
        "url": links.get("ly", ""),
        "urls": links,
        "note": f"飞机落地后需自行前往火车站，预留 {o['transfer_text']} 换乘",
    }


def build_options(direct: dict, transfers: list[dict],
                  dep_city: str, arr_city: str,
                  air_rail: list[dict] | None = None) -> list[dict]:
    options: list[dict] = []
    links = direct.get("links", {})
    for leg in direct.get("trains", []):
        options.append(_train_option(leg, dep_city, arr_city, links))
    for f in direct.get("flights", []):
        options.append(_flight_option(f, dep_city, arr_city, links))
    for o in transfers:
        options.append(_transfer_option(o, links))
    for o in (air_rail or []):
        options.append(_air_rail_option(o, links))
    return options


def _pareto_frontier(options: list[dict],
                     price_key: str = "price",
                     time_key: str = "duration_min") -> list[dict]:
    """Pareto 前沿：只保留"更便宜且更快"的选项，被支配的丢弃。

    判定规则：选项 A 支配 B ⟺ A 的价格 ≤ B 且 A 的耗时 ≤ B，且至少一项严格更优。
    因此被剔除的都是"既比某个方案贵、又比它慢"的方案——对用户毫无价值。

    注意：这里必须用 **累计最优** 来判定，而不是只跟"已保留的任意一个"比。
    排序后按下标递增扫描时，价格单调不减；但只要维护住
    "此前见过的最短耗时"，就能用 O(n log n)（排序 + 一次线性扫描）拿到真正的
    前沿，避免 O(n²) 的双重循环。
    """
    if not options:
        return []
    data = [o for o in options
            if isinstance(o.get(price_key), (int, float))
            and isinstance(o.get(time_key), (int, float))]
    if not data:
        return list(options)

    # 价格升序；同价格时耗时升序，保证便宜且快的先出现
    data.sort(key=lambda o: (o[price_key], o[time_key]))
    frontier: list[dict] = []
    best_time = float("inf")
    for o in data:
        t = o[time_key]
        if t < best_time:            # 耗时更短才可能进入前沿
            frontier.append(o)
            best_time = t
        # 耗时 >= best_time 说明已被"更便宜且不慢"的方案支配，丢弃
    return frontier


def dedup_options(options: list[dict]) -> list[dict]:
    """去掉重复方案。

    同一趟车可能被多个车站组合查出（如 北京→广州 会查 北京西/北京/北京南 × 广州/广州南），
    得到的是"同一车次 + 同一时刻 + 同席别"的重复条目，展示多份没有意义。

    去重键在字段缺失时自动退化（用 title/route/出发时刻兜底），
    保证对不同来源构造的 option 都能工作。
    """
    best: dict[tuple, dict] = {}
    for o in options:
        legs = o.get("legs") or []
        if legs:
            leg_key = tuple((l.get("code"), l.get("depart")) for l in legs)
        else:
            leg_key = (o.get("title"), o.get("depart"), o.get("route"))
        key = (o.get("mode") or o.get("kind"), leg_key, o.get("seat"))
        cur = best.get(key)
        if cur is None or o.get("price", 1e9) < cur.get("price", 1e9):
            best[key] = o
    return list(best.values())


def rank(options: list[dict], sort: str = "price", limit: int = 40,
         pareto: bool = False) -> list[dict]:
    """排序并编号。

    pareto=True 时先做 Pareto 前沿剪枝（见 _pareto_frontier），
    只保留"没有更便宜且更快的替代方案"的选项，再按 sort 排序。
    """
    options = dedup_options(options)
    if pareto:
        options = _pareto_frontier(options)
    if sort == "duration":
        options = sorted(options, key=lambda o: (o["duration_min"], o["price"]))
    elif sort == "depart":
        options = sorted(options, key=lambda o: (_time_key(o["depart"]), o["price"]))
    elif sort == "transfers":
        options = sorted(options, key=lambda o: (o["transfers"], o["price"]))
    elif sort == "value":
        # 性价比：每元换取的"到达速度"，兼顾价格与耗时
        options = sorted(options, key=lambda o: (o["price"] * (1 + o["duration_min"] / 120.0)))
    else:
        options = sorted(options, key=lambda o: (o["price"], o["duration_min"]))
    for i, o in enumerate(options, 1):
        o["rank"] = i
    return options[:limit]


def filter_options(options: list[dict], modes: list[str] | None = None,
                   only_direct: bool = False, budget: float | None = None,
                   depart_from: str | None = None, depart_to: str | None = None,
                   seat_rank_min: int = 0) -> list[dict]:
    out = []
    for o in options:
        if modes and o["kind"] not in modes:
            continue
        if only_direct and o["transfers"] > 0:
            continue
        if budget is not None and o["price"] > budget:
            continue
        if depart_from and _time_key(o["depart"]) < _time_key(depart_from):
            continue
        if depart_to and _time_key(o["depart"]) > _time_key(depart_to):
            continue
        out.append(o)
    return out


def summarize(options: list[dict]) -> dict:
    if not options:
        return {"count": 0}
    prices = [o["price"] for o in options]
    trains = [o for o in options if o["mode"] == "train"]
    flights = [o for o in options if o["mode"] == "flight"]
    cheapest = min(options, key=lambda o: o["price"])
    fastest = min(options, key=lambda o: o["duration_min"])
    best_value = min(options, key=lambda o: o["price"] * (1 + o["duration_min"] / 120.0))

    def lowest(rows):
        return min((r["price"] for r in rows), default=None)

    return {
        "count": len(options),
        "price_min": min(prices),
        "price_max": max(prices),
        "cheapest": cheapest,
        "fastest": fastest,
        "best_value": best_value,
        "train_count": len(trains),
        "flight_count": len(flights),
        "transfer_count": len([o for o in options if o["transfers"] > 0]),
        "train_price_min": lowest(trains),
        "flight_price_min": lowest(flights),
        "live_count": len([o for o in options if o.get("live")]),
        "estimate_count": len([o for o in options if not o.get("live")]),
        "saving_vs_next": (lambda s: round(s[1] - s[0], 1) if len(s) > 1 and s[1] > s[0] else 0)(
            sorted(prices)),
    }


def comparison_table(options: list[dict]) -> dict:
    """按"火车 vs 飞机"给出对比结论，用于界面顶部摘要。"""
    trains = [o for o in options if o["mode"] == "train"]
    flights = [o for o in options if o["mode"] == "flight"]
    rows = []
    for label, rows_ in (("火车", trains), ("飞机", flights)):
        if not rows_:
            continue
        cheap = min(rows_, key=lambda o: o["price"])
        fast = min(rows_, key=lambda o: o["duration_min"])
        rows.append({
            "mode": label,
            "count": len(rows_),
            "min_price": cheap["price"],
            "min_price_title": cheap["title"],
            "min_duration": fast["duration_min"],
            "min_duration_text": duration_text(fast["duration_min"]),
            "min_duration_title": fast["title"],
            "avg_price": round(sum(r["price"] for r in rows_) / len(rows_), 1),
        })
    conclusion = ""
    if len(rows) == 2:
        t, f = rows[0], rows[1]
        diff = round(f["min_price"] - t["min_price"], 1)
        if diff > 0:
            conclusion = (f"最低价：火车（{t['mode']} ¥{t['min_price']:.0f}）比飞机便宜 ¥{diff:.0f}；"
                          f"最快：{'火车' if t['min_duration'] < f['min_duration'] else '飞机'}"
                          f"（{min(t['min_duration'], f['min_duration'])} 分钟）")
        elif diff < 0:
            conclusion = (f"最低价：飞机 ¥{f['min_price']:.0f}，比火车便宜 ¥{abs(diff):.0f}")
        else:
            conclusion = "火车与飞机最低价持平"
    elif rows:
        conclusion = f"仅检索到{rows[0]['mode']}方案 {rows[0]['count']} 条"
    return {"rows": rows, "conclusion": conclusion}
