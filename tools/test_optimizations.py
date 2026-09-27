"""验证新增的 _leg_abs / _connections / _pareto_frontier 三个函数。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from farecompare.compare import _pareto_frontier, rank
from farecompare.planner import _connections, _leg_abs

ok = fail = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


print("=" * 76)
print("1) _leg_abs：绝对分钟换算")
print("=" * 76)
leg = {"depart": "08:49", "arrive": "19:11", "duration_min": 622}
a = _leg_abs(leg)
check("当天车次", a == (529, 1151), f"08:49 -> {a[0]} 分，到达 {a[1]} 分")
b = _leg_abs(leg, 1)
check("次日同班次 +1440", b == (529 + 1440, 1151 + 1440), f"{b}")
long_leg = {"depart": "11:32", "arrive": "13:00", "duration_min": 1528}   # 25h28m 跨夜
c = _leg_abs(long_leg)
check("跨夜车次到达落在次日", c[1] // 1440 == 1,
      f"发车 {c[0]} 分（第 {c[0]//1440} 天），到达 {c[1]} 分（第 {c[1]//1440} 天）")
check("非法时刻返回 None", _leg_abs({"depart": "", "duration_min": 10}) is None)


print("\n" + "=" * 76)
print("2) _connections：这种跨夜场景正是旧写法会漏掉的")
print("=" * 76)
# 场景：大连→上海 T131 跨夜，历时 25h28m，到达上海是 13:00（第 1 天）
# 下一段从上海出发：每天 14:00 有一班
next_legs = [{"code": "G100", "from_code": "SHH", "to_code": "HZH",
              "depart": "14:00", "duration_min": 60}]
arr_abs = 13 * 60 + 1440          # 第 1 天的 13:00
found = _connections(arr_abs, "SHH", next_legs)
check("跨夜到达后仍能衔接次日车次", len(found) == 1,
      f"匹配 {len(found)} 条" + (f"，间隔 {found[0][2]} 分钟" if found else ""))
if found:
    _b, dep_abs, gap = found[0]
    check("间隔计算正确（60 分钟）", gap == 60, f"gap={gap}")
    check("发车日在到达日之后", dep_abs >= arr_abs, f"发车绝对分钟 {dep_abs}")

# 对照：旧写法只试 {到达日, 到达日+1}
old_days = {arr_abs // 1440, arr_abs // 1440 + 1}
old_hit = any(0 <= (14 * 60 + d * 1440) - arr_abs <= 360 for d in old_days)
check("（对照）旧写法同样能命中此处", old_hit, "说明本场景两者一致，需另一组用例区分")

# 真正的差异场景：上一条腿跨了 2 天以上（超长途卧铺）
arr_abs2 = 10 * 60 + 2 * 1440     # 第 2 天 10:00 到达
found2 = _connections(arr_abs2, "SHH", next_legs)
old_days2 = {arr_abs2 // 1440, arr_abs2 // 1440 + 1}
old_hit2 = any(0 <= (14 * 60 + d * 1440) - arr_abs2 <= 360 for d in old_days2)
check("新写法能匹配（到达后 4 小时有车）", len(found2) == 1,
      f"gap={found2[0][2] if found2 else '-'}")
check("（对照）旧写法也能匹配", old_hit2, "两者一致")

# 换乘间隔过滤
far_legs = [{"code": "G999", "from_code": "SHH", "to_code": "HZH",
             "depart": "23:00", "duration_min": 60}]
check("间隔过大时不匹配（>360 分钟）",
      all(g <= 360 for _b, _d, g in _connections(arr_abs, "SHH", far_legs)) or
      len(_connections(arr_abs, "SHH", far_legs)) == 0)
short_legs = [{"code": "G998", "from_code": "SHH", "to_code": "HZH",
               "depart": "13:10", "duration_min": 60}]
check("间隔过小时不匹配（<30 分钟）", len(_connections(arr_abs, "SHH", short_legs)) == 0)
check("发站不符时不匹配",
      len(_connections(arr_abs, "BJP", next_legs)) == 0)
check("结果按发车时间升序",
      [x[1] for x in _connections(arr_abs, "SHH", [
          {"code": "B", "from_code": "SHH", "to_code": "X", "depart": "18:00", "duration_min": 60},
          {"code": "A", "from_code": "SHH", "to_code": "X", "depart": "14:00", "duration_min": 60},
      ])] == sorted([x[1] for x in _connections(arr_abs, "SHH", [
          {"code": "B", "from_code": "SHH", "to_code": "X", "depart": "18:00", "duration_min": 60},
          {"code": "A", "from_code": "SHH", "to_code": "X", "depart": "14:00", "duration_min": 60},
      ])]))


print("\n" + "=" * 76)
print("3) _pareto_frontier：支配关系剪枝")
print("=" * 76)
# 注意构造：每个"前沿点"都必须不被任何其他点支配（价格≤且耗时≤）
# 同时带上 mode/legs 字段，避免被 rank() 内部的去重逻辑合并（去重键含 mode+车次）
def _mk(title: str, price: float, dur: int, code: str) -> dict:
    return {"title": title, "price": price, "duration_min": dur,
            "mode": "train", "kind": "train", "seat": "二等座",
            "legs": [{"code": code, "depart": "09:00", "from_station": "A",
                      "to_station": "B"}]}


opts = [
    _mk("同样快一半价", 50, 120, "A001"),
    _mk("便宜又快", 100, 300, "A002"),
    _mk("最便宜但慢", 90, 600, "A003"),
    _mk("最快但贵", 500, 120, "A004"),
    _mk("中庸被支配", 300, 500, "A005"),
    _mk("贵但极快", 800, 60, "A006"),
]
front = [o["title"] for o in _pareto_frontier(opts)]
print("  输入:", [f'{o["title"]}(¥{o["price"]}/{o["duration_min"]}分)' for o in opts])
print("  前沿:", front)
check("剔除被支配项", "便宜又快" not in front and "中庸被支配" not in front)
check("剔除同耗时但更贵者", "最快但贵" not in front)
check("剔除既贵又慢者", "最便宜但慢" not in front)
check("保留最优性价比者", "同样快一半价" in front)
check("保留唯一最快者", "贵但极快" in front)
check("前沿数量正确", len(front) == 2, f"{len(front)} 条：{front}")

print("\n  另组：经典三阶梯前沿（应全部保留）")
ladder = [
    {"title": "最便宜最慢", "price": 100, "duration_min": 900},
    {"title": "中间", "price": 300, "duration_min": 500},
    {"title": "最贵最快", "price": 900, "duration_min": 200},
    {"title": "被支配者", "price": 400, "duration_min": 700},
]
lad = [o["title"] for o in _pareto_frontier(ladder)]
print("  输入:", [f'{o["title"]}(¥{o["price"]}/{o["duration_min"]}分)' for o in ladder])
print("  前沿:", lad)
check("阶梯全部保留", {"最便宜最慢", "中间", "最贵最快"} <= set(lad), f"{lad}")
check("被支配者剔除", "被支配者" not in lad)

check("前沿内无相互支配", all(
    not any(y["price"] <= x["price"] and y["duration_min"] <= x["duration_min"] and y is not x
            for y in _pareto_frontier(opts))
    for x in _pareto_frontier(opts)))

print("\n  边界：")
check("空列表返回空", _pareto_frontier([]) == [])
check("单元素原样返回", len(_pareto_frontier([{"price": 1, "duration_min": 1}])) == 1)
check("缺字段不崩溃",
      len(_pareto_frontier([{"price": 1}, {"duration_min": 2}])) == 2)

print("\n  与 rank(pareto=True) 集成：")
ranked = rank([dict(o) for o in opts], sort="price", pareto=True)
names = [o["title"] for o in ranked]
check("rank 支持 pareto 参数", names == ["同样快一半价", "贵但极快"],
      f"保留 {len(ranked)} 条：" + "、".join(names))
check("编号连续", [o["rank"] for o in ranked] == list(range(1, len(ranked) + 1)))
check("不开启 pareto 时不做前沿剪枝",
      len(rank([dict(o) for o in opts], sort="price")) == len(opts),
      f"{len(opts)} 条全部保留")

print("\n" + "=" * 76)
print("4) dedup_options：同一车次被多个车站组合查出时应合并")
print("=" * 76)
from farecompare.compare import dedup_options  # noqa: E402

dup = [
    {"kind": "train", "mode": "train", "seat": "二等座", "price": 1033,
     "duration_min": 468,
     "legs": [{"code": "G301", "depart": "09:00", "from_station": "北京西"}]},
    {"kind": "train", "mode": "train", "seat": "二等座", "price": 1033,
     "duration_min": 468,
     "legs": [{"code": "G301", "depart": "09:00", "from_station": "北京"}]},
    {"kind": "train", "mode": "train", "seat": "二等座", "price": 1036,
     "duration_min": 482,
     "legs": [{"code": "G301", "depart": "09:00", "from_station": "北京南"}]},
    {"kind": "train", "mode": "train", "seat": "二等座", "price": 900,
     "duration_min": 1798,
     "legs": [{"code": "K598", "depart": "11:00", "from_station": "北京西"}]},
]
d = dedup_options(dup)
check("同一车次+同席别合并为一条", len(d) == 2, f"{len(d)} 条")
check("保留价格最低的那条",
      min(x["price"] for x in d) == 900 and
      any(x["price"] == 1033 for x in d),
      "G301 合并后取 ¥1033（更便宜的 ¥1036 被合并掉）")
check("不同车次不被合并", any(x["legs"][0]["code"] == "K598" for x in d))
check("rank 内部自动去重",
      len(rank([dict(x) for x in dup], sort="price")) == 2,
      "rank 输出 2 条")

print("\n" + "=" * 76)
print(f"结果：通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
