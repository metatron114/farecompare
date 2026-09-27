"""对比 Pareto 剪枝开关的实际效果（用真实搜索数据）。"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8765"


def post(p, b):
    r = urllib.request.Request(BASE + p, data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())


def get(p):
    return json.loads(urllib.request.urlopen(BASE + p, timeout=60).read())


def run(payload):
    tid = post("/api/search", payload)["task_id"]
    while True:
        if get("/api/progress?task_id=" + tid)["status"] != "running":
            break
        time.sleep(0.5)
    return get("/api/result?task_id=" + tid).get("result") or {}


day = (date.today() + timedelta(days=10)).isoformat()
base = {"from": "北京", "to": "广州", "date": day, "include_train": True,
        "include_flight": True, "allow_transfer": True,
        "exclude_no_baggage": True, "no_cache": True, "sort": "price"}

print("=" * 78)
print(f"北京 → 广州  {day}")
print("=" * 78)

off = run({**base, "pareto": False})
print(f"\n【关闭 Pareto】方案 {off['summary']['count']} 条")
prices = [o["price"] for o in off["options"]]
durs = [o["duration_min"] for o in off["options"]]
print(f"  价格区间 ¥{min(prices):.0f} ~ ¥{max(prices):.0f}")
print(f"  耗时区间 {min(durs)//60}h{min(durs)%60:02d}m ~ {max(durs)//60}h{max(durs)%60:02d}m")
for o in off["options"][:6]:
    print(f"    [{o['mode_label']:<5s}] ¥{o['price']:>7.0f}  "
          f"{o['duration']:>11s}  {o['title'][:32]}")
print("    ...")

on = run({**base, "pareto": True})
print(f"\n【开启 Pareto】方案 {on['summary']['count']} 条"
      f"（剪掉 {on.get('pareto_pruned')} 条被支配方案）")
for o in on["options"]:
    print(f"    [{o['mode_label']:<5s}] ¥{o['price']:>7.0f}  "
          f"{o['duration']:>11s}  {o['title'][:32]}")

print("\n" + "=" * 78)
print("剪枝正确性校验")
print("=" * 78)
kept = {(o["price"], o["duration_min"]) for o in on["options"]}
removed = [o for o in off["options"] if (o["price"], o["duration_min"]) not in kept]
print(f"  被剪掉的 {len(removed)} 条，逐条确认是否存在更便宜且更快的替代方案：")
bad = 0
for o in removed[:10]:
    dom = [k for k in kept if k[0] <= o["price"] and k[1] <= o["duration_min"]
           and (k[0] < o["price"] or k[1] < o["duration_min"])]
    dur_txt = f"{o['duration_min'] // 60}h{o['duration_min'] % 60:02d}m"
    if dom:
        d0 = dom[0]
        d_txt = f"¥{d0[0]:.0f}/{d0[1] // 60}h{d0[1] % 60:02d}m"
        print(f"    ✓ ¥{o['price']:.0f}/{dur_txt} 被 {d_txt} 支配")
    else:
        bad += 1
        print(f"    ✗ ¥{o['price']:.0f}/{dur_txt} 未被支配（误剪！）")
print(f"\n  误剪数量: {bad}  {'✅ 无任何误剪' if bad == 0 else '❌ 存在误剪'}")

print(f"\n  最低价是否保留: "
      f"{'✅' if min(prices) in {o['price'] for o in on['options']} else '❌'} "
      f"（¥{min(prices):.0f}）")
fastest = min(durs)
print(f"  最快是否保留:   "
      f"{'✅' if fastest in {o['duration_min'] for o in on['options']} else '❌'} "
      f"（{fastest//60}h{fastest%60:02d}m）")
