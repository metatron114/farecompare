"""端到端验收：数据源、双通道搜索、多车站覆盖、偏僻站点中转、区间搜索。

用法：先启动服务，再运行
    python -m farecompare serve --no-browser     # 另一个终端
    python tools/acceptance.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("FARECOMPARE_URL", "http://127.0.0.1:8765")
ok = fail = 0


def verify(label: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


def get(p: str, timeout: int = 60):
    with urllib.request.urlopen(BASE + p, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(p: str, body: dict):
    r = urllib.request.Request(BASE + p, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(payload: dict, path: str = "/api/search"):
    t0 = time.time()
    tid = post(path, payload)["task_id"]
    while True:
        if get("/api/progress?task_id=" + tid)["status"] != "running":
            break
        time.sleep(0.6)
    return get("/api/result?task_id=" + tid), time.time() - t0


def main() -> int:
    today = date.today()
    day = (today + timedelta(days=10)).isoformat()

    print("=" * 80)
    print("farecompare 端到端验收")
    print("=" * 80)

    try:
        h = get("/api/health")
    except Exception as exc:
        print(f"[错误] 无法连接服务 {BASE} —— {exc}")
        print("       请先启动： python -m farecompare serve --no-browser")
        return 1

    print("\n1) 数据源状态")
    gt = h.get("train_gaotie") or {}
    verify("gaotie 车次通道可用", gt.get("reachable") is True, gt.get("name", ""))
    verify("车站拼音标识已载入", (gt.get("slugs") or 0) > 5000, f"{gt.get('slugs')} 个")
    cat = h.get("passenger_catalog") or {}
    verify("客运车站目录可用", cat.get("available") is True,
           f"{cat.get('stations')} 个客运站，剔除货运站 {cat.get('excluded_stations')} 个")
    verify("机票实时源可用", h["flights"]["ly"]["live_data"] is True)

    print("\n2) 非 12306 主通道：速度与 12306 配额")
    out, el = run({"from": "上海", "to": "北京", "date": day, "include_train": True,
                   "include_flight": False, "allow_transfer": False, "no_cache": True})
    r = out.get("result") or {}
    srcs = r.get("train_sources") or {}
    print(f"  上海→北京 {el:.1f}s  方案 {len(r.get('options') or [])} 条  来源 {srcs}")
    verify("车次来自 gaotie", srcs.get("gaotie.com.cn", 0) > 0, str(srcs))
    verify("12306 请求受控（<8 次）", (r.get("requests") or 0) < 8,
           f"{r.get('requests')} 次")
    verify("响应速度（<20s）", el < 20, f"{el:.1f}s")
    prices = [o["price"] for o in r.get("options", [])]
    verify("按价格升序", prices == sorted(prices))

    print("\n3) 同城多车站覆盖")
    out2, _ = run({"from": "大连", "to": "上海", "date": day, "include_train": True,
                   "include_flight": False, "allow_transfer": False, "no_cache": True})
    r2 = out2.get("result") or {}
    dep_st = sorted({o["legs"][0]["from_station"] for o in r2.get("options", [])})
    verify("覆盖多个出发车站", len(dep_st) >= 2, "、".join(dep_st))

    print("\n4) 偏僻站点经省会/枢纽中转")
    out3, el3 = run({"from": "喀什", "to": "北京", "date": day, "include_train": True,
                     "include_flight": False, "allow_transfer": True, "no_cache": True})
    r3 = out3.get("result") or {}
    tr = [o for o in r3.get("options", []) if o["kind"] == "transfer"]
    hubs = sorted({o["hub"] for o in tr})
    print(f"  喀什→北京 {el3:.1f}s  中转 {len(tr)} 条  经 {hubs}")
    verify("给出中转方案", bool(tr), f"{len(tr)} 条")
    verify("经省会/大枢纽", any("乌鲁木齐" in x for x in hubs), "、".join(hubs))
    verify("两段车站首尾相接",
           all(o["legs"][0]["to_station"] == o["legs"][1]["from_station"] for o in tr))

    print("\n5) 车站分组与过滤")
    g = get("/api/suggest?q=" + urllib.request.quote("大连") + "&grouped=1&limit=3")
    grp = g["items"][0]
    names = [s["name"] for s in grp["stations"]]
    verify("城市分组返回子车站", {"大连", "大连北"} <= set(names), "、".join(names[:6]))
    verify("货运站已剔除", "大连西" not in names)

    print("\n6) 功能回归")
    out4, el4 = run({"from": "北京", "to": "广州", "date": day, "include_train": True,
                     "include_flight": True, "allow_transfer": True,
                     "exclude_no_baggage": True, "no_cache": True})
    r4 = out4.get("result") or {}
    verify("单日比价含实时机票", r4.get("flight_live") is True,
           f"{len(r4.get('options', []))} 条方案, {el4:.1f}s")
    s = (today + timedelta(days=6)).isoformat()
    e = (today + timedelta(days=16)).isoformat()
    out5, el5 = run({"from": "上海", "to": "成都", "start": s, "end": e,
                     "include_train": False, "train_days": 0}, "/api/range")
    r5 = out5.get("result") or {}
    verify("区间每日最低价", len(r5.get("days") or []) >= 8,
           f"{len(r5.get('days') or [])} 天, {el5:.1f}s")

    print("\n" + "=" * 80)
    print(f"验收结果：通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
