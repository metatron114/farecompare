"""自检：验证数据源、核心逻辑与本地服务是否正常。

用法：
    python tools/selftest.py            # 数据源与离线逻辑
    python tools/selftest.py --api      # 额外测试已启动的本地服务
    python tools/selftest.py --no-browser   # 跳过需要 Edge 的机票抓取
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, WARN = "[通过]", "[失败]", "[注意]"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"{status} {name}" + (f" — {detail}" if detail else ""))


def check(name: str, fn) -> bool:
    try:
        detail = fn()
        record(PASS, name, detail or "")
        return True
    except Exception as exc:
        record(FAIL, name, f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true", help="测试本地服务接口")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="跳过需要 Edge 的机票测试")
    args = ap.parse_args()

    print("=" * 70)
    print("车票 / 机票 比价搜索 —— 自检")
    print("=" * 70)

    from farecompare import __version__, airports, china_geo, ly_cities, passenger_catalog
    from farecompare.compare import build_options, comparison_table, rank, summarize
    from farecompare.planner import JourneyPlanner
    from farecompare.provider_12306 import Ticket12306
    from farecompare.provider_flight import search_flights, search_range
    from farecompare.provider_gaotie import route_trains as gaotie_route
    from farecompare.stations import get_stations

    day = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    record(PASS, "包版本", f"farecompare {__version__}")

    # 1) 车站库与地理数据
    st = get_stations()
    check("车站库加载", lambda: f"{len(st.by_name)} 个铁路车站，来源 {st.source}")
    check("车站解析（中文站名）", lambda: f"上海虹桥 -> {st.code_of('上海虹桥')}")
    check("车站解析（城市名）", lambda: f"北京 -> {st.code_of('北京')}")
    check("城市->车站子分组",
          lambda: f"大连 {len(st.stations_of_city('大连'))} 站、北京 {len(st.stations_of_city('北京'))} 站")
    cat = passenger_catalog.stats()
    excluded = sum(1 for e in st.by_name.values() if not e.get("passenger", True))
    check("客运车站目录（过滤货运站）",
          lambda: f"{cat['stations']} 个客运站，已剔除货运站 {excluded} 个")
    geo = china_geo.describe()
    check("城市规划库（省会/枢纽）",
          lambda: f"{geo['provinces']} 个省级区划，{geo['hubs']} 个枢纽；"
                  f"大连所在省会 = {china_geo.capital_of('大连')}")
    check("民航城市库", lambda: f"{len(ly_cities.all_cities())} 个城市，"
                              f"{len(ly_cities.all_airports())} 个机场条目")

    # 2) gaotie 车次通道（非 12306）
    def tgaotie():
        res = gaotie_route("上海", "北京")
        if not res.get("trains"):
            raise RuntimeError(res.get("error") or "未取到车次")
        t = res["trains"][0]
        return (f"上海→北京 {len(res['trains'])} 趟，首趟 {t['code']} "
                f"{t['depart']}-{t['arrive']} {t['seat']} ¥{t['price']:.0f}")

    check("gaotie 车次通道（主搜索通道）", tgaotie)

    # 3) 12306 通道
    client = Ticket12306(min_interval=20.0)
    client.set_name_resolver(st.name_of_code)
    box: dict = {}

    def t12306():
        rows = client.query_tickets(st.code_of("北京"), st.code_of("上海"), day)
        box["rows"] = rows
        if not rows:
            raise RuntimeError("未取到车次（可能未起售或网络异常）")
        return f"{day} 北京→上海 有效车次 {len(rows)} 列，首列 {rows[0]['code']}"

    check("12306 余票查询（补充通道）", t12306)

    def tprice():
        rows = box.get("rows") or []
        if not rows:
            raise RuntimeError("无可用车次，跳过")
        priced = client.query_price_many(rows[:3], limit=3)
        got = [(r["code"], r.get("prices")) for r in priced if r.get("prices")]
        if not got:
            raise RuntimeError("票价接口未返回价格")
        code, prices = got[0]
        return f"{code} 席别 {len(prices)} 项，最低 {prices[0]['seat']} ¥{prices[0]['price']}"

    check("12306 票价查询（实时价格）", tprice)

    # 4) 机票
    if args.no_browser:
        print(f"{WARN} 已跳过机票实时抓取（--no-browser）")
    else:
        def tly():
            res = search_flights("北京", "上海", day, exclude_no_baggage=True)
            if res["source"] != "ly":
                raise RuntimeError(f"未取到同程实时数据（source={res['source']}）")
            first = res["flights"][0]
            return (f"同程实时 {len(res['flights'])} 个航班，最低 {first['flight_no']} "
                    f"{first['airline']} ¥{first['price']:.0f}；"
                    f"已屏蔽无托运 {len(res['excluded_no_baggage'])} 个")

        check("机票实时抓取（同程，Edge/CDP）", tly)

        def tcal():
            r = search_range("北京", "上海",
                             (datetime.date.today() + datetime.timedelta(days=5)).isoformat(),
                             (datetime.date.today() + datetime.timedelta(days=18)).isoformat())
            if not r.get("days"):
                raise RuntimeError(r.get("error") or "价格日历为空")
            c = r.get("cheapest") or {}
            return f"区间 {len(r['days'])} 天，最低 {c.get('date')} ¥{c.get('price')}"

        check("日期区间价格日历", tcal)

    # 5) 规划与排序
    def tplanner():
        p = JourneyPlanner(client=client)
        d = p.direct("北京", "上海", day, include_train=True, include_flight=False)
        opts = build_options(d, [], "北京", "上海")
        ordered = rank(opts, "price")
        s = summarize(ordered)
        c = comparison_table(ordered)
        if not ordered:
            raise RuntimeError("未生成候选方案")
        return (f"候选 {len(ordered)} 条，最低 ¥{s['price_min']}，"
                f"首项 {ordered[0]['title']}；{c['conclusion'][:34]}")

    check("直达规划 + 比价排序", tplanner)

    # 6) 本地服务
    if args.api:
        base = f"http://127.0.0.1:{args.port}"

        def tindex():
            with urllib.request.urlopen(base + "/", timeout=10) as r:
                body = r.read()
            if b"<html" not in body.lower():
                raise RuntimeError("首页不是 HTML")
            return f"首页 {len(body)} 字节"

        check("服务首页", tindex)

        def thealth():
            with urllib.request.urlopen(base + "/api/health", timeout=30) as r:
                h = json.loads(r.read().decode("utf-8"))
            gt = h.get("train_gaotie") or {}
            return (f"车站库 {h['stations']['count']}；12306 请求 {h['train_requests']} 次/"
                    f"冷却 {h['train_cooldowns']} 次；gaotie {gt.get('slugs')} 个 slug；"
                    f"同程 live={h['flights']['ly']['live_data']}")

        check("服务健康接口", thealth)

        def tsuggest():
            q = urllib.parse.quote("广州")
            with urllib.request.urlopen(base + "/api/suggest?q=" + q, timeout=10) as r:
                j = json.loads(r.read().decode("utf-8"))
            if not j.get("items"):
                raise RuntimeError("无联想结果")
            return f"广州 -> {j['items'][0]['name']}（{j['items'][0]['code']}）"

        check("车站联想接口", tsuggest)
    else:
        print(f"{WARN} 未测试本地服务（加 --api 参数可测试，需先启动服务）")

    print("-" * 70)
    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print(f"合计 {len(results)} 项：通过 {len(results) - len(fails) - len(warns)}，"
          f"失败 {len(fails)}，注意 {len(warns)}")
    if fails:
        for _, name, detail in fails:
            print(f"  {FAIL} {name}: {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
