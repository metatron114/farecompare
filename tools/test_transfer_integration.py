"""验证接入 _connections 后的中转行为（含隔夜、同日、跨夜长车次）。"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from farecompare.planner import JourneyPlanner
from farecompare.provider_12306 import Ticket12306

ok = fail = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


print("=" * 78)
print("1) 单元级：_match_pairs 的三种场景")
print("=" * 78)
p = JourneyPlanner(client=Ticket12306(min_interval=30.0))

# ① 同日衔接
ins = [{"code": "G1", "from_code": "A", "to_code": "H",
        "depart": "08:00", "arrive": "12:00", "duration_min": 240}]
outs = [{"code": "G2", "from_code": "H", "to_code": "B",
         "depart": "13:00", "arrive": "15:00", "duration_min": 120}]
r = p._match_pairs(ins, outs)
check("同日衔接配对成功", len(r) == 1 and r[0][2] == 60 and r[0][3] is False,
      f"{[(x[0]['code'], x[1]['code'], x[2], x[3]) for x in r]}")

# ② 前段跨夜 8 小时（夕发朝至），到达次日 06:00，后段次日 08:00 出发
ins2 = [{"code": "Z1", "from_code": "A", "to_code": "H",
         "depart": "22:00", "arrive": "06:00", "duration_min": 480}]
outs2 = [{"code": "G3", "from_code": "H", "to_code": "B",
          "depart": "08:00", "arrive": "10:00", "duration_min": 120}]
r2 = p._match_pairs(ins2, outs2)
check("夕发朝至（跨夜 8h）配对", len(r2) == 1 and r2[0][2] == 120 and r2[0][3] is False,
      f"间隔 {r2[0][2]} 分钟" if r2 else "未配对")

# ③ 前段跨夜超 24 小时（T131 型 25h28m）——旧启发式会算错
ins3 = [{"code": "T131", "from_code": "DLT", "to_code": "SHH",
         "depart": "11:32", "arrive": "13:00", "duration_min": 1528}]
outs3 = [{"code": "G4", "from_code": "SHH", "to_code": "HZH",
          "depart": "14:00", "arrive": "15:00", "duration_min": 60}]
r3 = p._match_pairs(ins3, outs3)
check("超长跨夜（25h28m）配对且间隔正确",
      len(r3) == 1 and r3[0][2] == 60,
      f"间隔 {r3[0][2]} 分钟（旧写法会算成 -1436 分钟而漏配）" if r3 else "未配对")

# ④ 隔夜方案：当天到站晚，次日早晨接续
ins4 = [{"code": "G5", "from_code": "A", "to_code": "H",
         "depart": "08:00", "arrive": "19:00", "duration_min": 660}]
outs4 = [{"code": "G6", "from_code": "H", "to_code": "B",
          "depart": "07:30", "arrive": "09:30", "duration_min": 120}]
r4 = p._match_pairs(ins4, outs4)
check("同日无解时给出隔夜方案",
      len(r4) == 1 and r4[0][3] is True,
      f"隔夜={r4[0][3]}，间隔 {r4[0][2]} 分钟（{r4[0][2] // 60}小时）" if r4 else "未配对")
if r4:
    check("隔夜间隔在 8~20 小时区间", 8 * 60 <= r4[0][2] <= 20 * 60, f"{r4[0][2]} 分钟")

# ⑤ 间隔过短 / 过长都不得配对
ins5 = [{"code": "G7", "from_code": "A", "to_code": "H",
         "depart": "08:00", "arrive": "12:00", "duration_min": 240}]
short = [{"code": "G8", "from_code": "H", "to_code": "B",
          "depart": "12:10", "arrive": "14:00", "duration_min": 110}]
check("间隔 10 分钟不配对", len(p._match_pairs(ins5, short)) == 0)
late = [{"code": "G9", "from_code": "H", "to_code": "B",
         "depart": "18:30", "arrive": "20:00", "duration_min": 90}]
check("间隔 390 分钟不配对（超过上限 360）", len(p._match_pairs(ins5, late)) == 0)
check("站码不符不配对",
      len(p._match_pairs(ins5, [{"code": "GX", "from_code": "X", "to_code": "B",
                                 "depart": "13:00", "arrive": "15:00",
                                 "duration_min": 120}])) == 0)

print("\n" + "=" * 78)
print("2) 真实数据：隔夜换乘是否仍能产出")
print("=" * 78)
day = (date.today() + timedelta(days=10)).isoformat()
client = Ticket12306()
planner = JourneyPlanner(client=client)
for dep, arr in [("呼和浩特", "广州"), ("喀什", "北京")]:
    client.begin_budget(30)
    client.clear_soft_limit()
    d = planner.direct(dep, arr, day, include_train=True, include_flight=False)
    tr = planner.transfer(dep, arr, day, max_options=3, hub_limit=4)
    print(f"\n  {dep} → {arr}   {day}")
    print(f"    直达 {len(d['trains'])} 条，中转 {len(tr)} 条")
    for o in tr:
        a, b = o["legs"]
        tag = "隔夜" if o.get("overnight") else "同日"
        print(f"      经 {o['hub']:<8s} [{tag}] ¥{o['price']:.0f} {o['duration']} "
              f"换乘 {o['transfer_text']}")
        print(f"         {a['code']:<8s} {a['from_station'][:6]}→{a['to_station'][:6]} "
              f"{a['depart']}-{a['arrive']} ¥{a['price']:.0f}")
        print(f"         {b['code']:<8s} {b['from_station'][:6]}→{b['to_station'][:6]} "
              f"{b['depart']}-{b['arrive']} ¥{b['price']:.0f}")
        # 关键校验：两段站码必须衔接
        check(f"{dep}→{arr} 经{o['hub']} 两段站码衔接",
              a["to_code"] == b["from_code"], f"{a['to_code']} == {b['from_code']}")
        check(f"{dep}→{arr} 经{o['hub']} 换乘间隔合理",
              30 <= o["transfer_min"] <= 20 * 60,
              f"{o['transfer_min']} 分钟")

print("\n" + "=" * 78)
print(f"结果：通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
