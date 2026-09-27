"""核对：后台任务与运行时学习对 passenger_stations.json 的并发写入是否损坏数据。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from farecompare.paths import cache_file
from farecompare.stations import get_stations
from farecompare import passenger_catalog

path = cache_file("passenger_stations.json")
print("文件:", path)
print("存在:", os.path.exists(path), "| 大小:", os.path.getsize(path) if os.path.exists(path) else 0)

data = json.load(open(path, encoding="utf-8"))
codes = data.get("codes") or []
print("来源:", data.get("source"))
print("记录数:", len(codes), "| total 字段:", data.get("total"))

st = get_stations()
st_codes = st.passenger_codes()
print("\n程序载入的实测客运站:", len(st_codes))
print("磁盘 codes 与载入集合一致:", set(codes) == st_codes)

print("\n样本站名:", "、".join(sorted(st.name_of_code(c) for c in codes)[:20]))

print("\n=== 过滤效果核对（重点城市）===")
for city in ["大连", "北京", "上海", "喀什", "三亚"]:
    names = st.stations_of_city(city)
    print(f"  {city}: {len(names)} 站 -> {'、'.join(names[:10])}")

print("\n=== 冲突检查：实测客运站是否被目录判为非客运 ===")
cat = passenger_catalog.load()
conflict = []
for c in st_codes:
    name = st.name_of_code(c)
    if cat.get("available") and name not in cat["stations"]:
        conflict.append(name)
print("冲突数:", len(conflict), conflict[:15] or "无")

print("\n=== 两个数据源的分工确认 ===")
print(f"  gaotie 客运目录 : {len(cat['stations'])} 个（主依据，随仓库分发）")
print(f"  实测学习名单    : {len(st_codes)} 个（缓存，运行中自动累积）")
print("  过滤优先级：实测名单 > 客运目录 > 保守保留")
