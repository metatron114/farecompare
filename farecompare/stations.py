"""车站与城市解析。

数据来源优先级：
1. 本地缓存 cache/stations.json（7 天有效）
2. 12306 官方站名表 https://kyfw.12306.cn/otn/resources/js/framework/station_name.js
3. 内置兜底站表（国内主要城市，保证离线可用）

12306 站名表格式：@bjb|北京北|VAP|beijingbei|bjb|0@bjd|北京东|BOP|...
字段：[简拼, 中文名, 电报码, 全拼, 拼音首字母, 序号, 城市代码?]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

from .paths import cache_dir, cache_file

BASE_DIR = os.path.dirname(cache_dir())
CACHE_DIR = cache_dir()
STATION_CACHE = cache_file("stations.json")
STATION_CACHE_TTL = 7 * 24 * 3600

STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"

PASSENGER_CACHE = cache_file("passenger_stations.json")

# 明显的非客运车站名称特征（货运站、编组站、线路所、乘降所、工区等）
NON_PASSENGER_KEYWORDS = (
    "货场", "货运", "编组", "线路所", "乘降所", "工区", "机务",
    "列检", "车辆段", "动车所", "物流", "码头",
)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 内置兜底：城市 -> [(站名, 电报码), ...]  用于断网/接口异常时仍可搜索
FALLBACK_STATIONS: dict[str, list[tuple[str, str]]] = {
    "北京": [("北京", "BJP"), ("北京南", "VNP"), ("北京西", "BXP"), ("北京北", "VAP"), ("北京朝阳", "IFP"), ("北京丰台", "FTP")],
    "上海": [("上海", "SHH"), ("上海虹桥", "AOH"), ("上海南", "SNH"), ("上海西", "SXH")],
    "广州": [("广州", "GZQ"), ("广州南", "IZQ"), ("广州东", "GGQ"), ("广州北", "GBQ")],
    "深圳": [("深圳", "SZQ"), ("深圳北", "IOQ"), ("深圳东", "BJQ"), ("福田", "NZQ")],
    "天津": [("天津", "TJP"), ("天津西", "TXP"), ("天津南", "TIP"), ("滨海", "YKP")],
    "重庆": [("重庆", "CQW"), ("重庆北", "CUW"), ("重庆西", "CXW"), ("沙坪坝", "CYW")],
    "成都": [("成都", "CDW"), ("成都东", "ICW"), ("成都南", "CNW"), ("成都西", "CMW")],
    "杭州": [("杭州", "HZH"), ("杭州东", "HGH"), ("杭州南", "XHH"), ("杭州西", "HVH")],
    "南京": [("南京", "NJH"), ("南京南", "NKH"), ("南京北", "NBH")],
    "武汉": [("武汉", "WHN"), ("汉口", "HKN"), ("武昌", "WCN"), ("武汉东", "LFN")],
    "西安": [("西安", "XAY"), ("西安北", "EAY"), ("西安南", "CAY")],
    "郑州": [("郑州", "ZZF"), ("郑州东", "ZAF"), ("郑州西", "XPF")],
    "济南": [("济南", "JNK"), ("济南西", "JGK"), ("济南东", "MDK")],
    "长沙": [("长沙", "CSQ"), ("长沙南", "CWQ")],
    "合肥": [("合肥", "HFH"), ("合肥南", "ENH")],
    "福州": [("福州", "FZS"), ("福州南", "FYS")],
    "厦门": [("厦门", "XMS"), ("厦门北", "XKS")],
    "南昌": [("南昌", "NCG"), ("南昌西", "NXG")],
    "沈阳": [("沈阳", "SYT"), ("沈阳北", "SBT"), ("沈阳南", "SNT")],
    "哈尔滨": [("哈尔滨", "HBB"), ("哈尔滨西", "HXB")],
    "长春": [("长春", "CCT"), ("长春西", "CRT")],
    "大连": [("大连", "DLT"), ("大连北", "DFT")],
    "青岛": [("青岛", "QDK"), ("青岛北", "QHK"), ("青岛西", "QEK")],
    "石家庄": [("石家庄", "SJP"), ("石家庄北", "VVP")],
    "太原": [("太原", "TYV"), ("太原南", "TNV")],
    "呼和浩特": [("呼和浩特", "HHC"), ("呼和浩特东", "NDC")],
    "兰州": [("兰州", "LZJ"), ("兰州西", "LAJ")],
    "西宁": [("西宁", "XNO")],
    "银川": [("银川", "YIJ")],
    "乌鲁木齐": [("乌鲁木齐", "WAR")],
    "拉萨": [("拉萨", "LSO")],
    "昆明": [("昆明", "KMM"), ("昆明南", "KOM")],
    "贵阳": [("贵阳", "GIW"), ("贵阳北", "KQW")],
    "南宁": [("南宁", "NNZ"), ("南宁东", "NFZ")],
    "海口": [("海口", "VUQ"), ("海口东", "KEQ")],
    "三亚": [("三亚", "SEQ")],
    "苏州": [("苏州", "SZH"), ("苏州北", "OHH")],
    "无锡": [("无锡", "WXH"), ("无锡东", "WGH")],
    "徐州": [("徐州", "XCH"), ("徐州东", "UUH")],
    "常州": [("常州", "CZH"), ("常州北", "ESH")],
    "宁波": [("宁波", "NGH"), ("宁波东", "NVH")],
    "温州": [("温州", "RZH"), ("温州南", "VRH")],
    "金华": [("金华", "JBH"), ("金华南", "RNH")],
    "嘉兴": [("嘉兴", "EAH"), ("嘉兴南", "EAH")],
    "南通": [("南通", "NUH")],
    "连云港": [("连云港", "UIH")],
    "烟台": [("烟台", "YAK")],
    "潍坊": [("潍坊", "WFK")],
    "淄博": [("淄博", "ZBK")],
    "洛阳": [("洛阳", "LYF"), ("洛阳龙门", "LLF")],
    "商丘": [("商丘", "SQF")],
    "信阳": [("信阳", "XUN")],
    "襄阳": [("襄阳", "XFN"), ("襄阳东", "XWN")],
    "宜昌": [("宜昌", "YCN"), ("宜昌东", "HAN")],
    "株洲": [("株洲", "ZZQ"), ("株洲西", "ZAQ")],
    "衡阳": [("衡阳", "HYQ"), ("衡阳东", "HVQ")],
    "郴州": [("郴州", "ICQ"), ("郴州西", "ICQ")],
    "桂林": [("桂林", "GLZ"), ("桂林北", "GBZ")],
    "柳州": [("柳州", "LZZ")],
    "佛山": [("佛山西", "FGQ")],
    "东莞": [("东莞", "RTQ"), ("东莞南", "IXQ")],
    "珠海": [("珠海", "ZHQ")],
    "中山": [("中山", "ZSQ")],
    "惠州": [("惠州", "HCQ")],
    "汕头": [("汕头", "OTQ")],
    "泉州": [("泉州", "QYS")],
    "莆田": [("莆田", "PTS")],
    "保定": [("保定", "BDP")],
    "唐山": [("唐山", "TSP")],
    "秦皇岛": [("秦皇岛", "QTP")],
    "沧州": [("沧州", "COP")],
    "廊坊": [("廊坊", "LJP")],
    "邯郸": [("邯郸", "HDP")],
    "张家口": [("张家口", "ZJP")],
    "承德": [("承德", "CDP")],
    "大同": [("大同", "DTV")],
    "临汾": [("临汾", "LFV")],
    "运城": [("运城", "YNV")],
    "包头": [("包头", "BTC")],
    "赤峰": [("赤峰", "CFD")],
    "通辽": [("通辽", "TLD")],
    "吉林": [("吉林", "JLL")],
    "延吉": [("延吉", "YJL")],
    "齐齐哈尔": [("齐齐哈尔", "QHX")],
    "牡丹江": [("牡丹江", "MDB")],
    "佳木斯": [("佳木斯", "JMB")],
    "锦州": [("锦州", "JZD")],
    "鞍山": [("鞍山", "AST")],
    "丹东": [("丹东", "DUT")],
    "营口": [("营口", "YKT")],
    "盘锦": [("盘锦", "PVD")],
    "盐城": [("盐城", "AFH")],
    "扬州": [("扬州", "YLH")],
    "镇江": [("镇江", "ZEH")],
    "泰州": [("泰州", "UTH")],
    "淮安": [("淮安", "AUH")],
    "宿迁": [("宿迁", "QOH")],
    "芜湖": [("芜湖", "WHH")],
    "蚌埠": [("蚌埠", "BBH")],
    "阜阳": [("阜阳", "FYH")],
    "安庆": [("安庆", "AQH")],
    "黄山": [("黄山北", "NYH")],
    "九江": [("九江", "JJG")],
    "赣州": [("赣州", "GZG")],
    "上饶": [("上饶", "SRG")],
    "景德镇": [("景德镇", "JCG")],
    "开封": [("开封", "KFF")],
    "新乡": [("新乡", "XXF")],
    "安阳": [("安阳", "AYF")],
    "南阳": [("南阳", "NFF")],
    "许昌": [("许昌", "XCF")],
    "漯河": [("漯河", "LNF")],
    "十堰": [("十堰", "SNN")],
    "荆州": [("荆州", "JBN")],
    "恩施": [("恩施", "ESN")],
    "岳阳": [("岳阳", "YYQ"), ("岳阳东", "YIQ")],
    "常德": [("常德", "VGQ")],
    "益阳": [("益阳", "YTQ")],
    "怀化": [("怀化", "HHQ"), ("怀化南", "KAQ")],
    "娄底": [("娄底", "LDQ")],
    "永州": [("永州", "AOQ")],
    "韶关": [("韶关", "SNQ"), ("韶关东", "SGQ")],
    "肇庆": [("肇庆", "ZVQ"), ("肇庆东", "FCQ")],
    "江门": [("江门", "JOQ")],
    "湛江": [("湛江", "ZJZ")],
    "茂名": [("茂名", "MMZ")],
    "梅州": [("梅州", "MOQ")],
    "揭阳": [("揭阳", "JRQ")],
    "潮州": [("潮州", "CBQ")],
    "漳州": [("漳州", "ZUS")],
    "龙岩": [("龙岩", "LYS")],
    "三明": [("三明", "SMS")],
    "南平": [("南平", "NPS")],
    "宁德": [("宁德", "NES")],
    "绵阳": [("绵阳", "MYW")],
    "德阳": [("德阳", "DYW")],
    "南充": [("南充", "NCW")],
    "宜宾": [("宜宾", "YBW")],
    "泸州": [("泸州", "LZW")],
    "乐山": [("乐山", "IVW")],
    "遵义": [("遵义", "ZIW")],
    "六盘水": [("六盘水", "UMW")],
    "曲靖": [("曲靖", "QJM")],
    "大理": [("大理", "DKM")],
    "丽江": [("丽江", "LHM")],
    "玉溪": [("玉溪", "AXM")],
    "红河": [("蒙自", "MZM")],
    "文山": [("文山", "WAM")],
    "天水": [("天水", "TSJ")],
    "嘉峪关": [("嘉峪关", "JGJ")],
    "张掖": [("张掖", "ZYJ")],
    "武威": [("武威", "WUJ")],
    "酒泉": [("酒泉", "JQJ")],
    "延安": [("延安", "YWY")],
    "宝鸡": [("宝鸡", "BJY")],
    "咸阳": [("咸阳", "XYY")],
    "渭南": [("渭南", "WNY")],
    "汉中": [("汉中", "HOY")],
    "安康": [("安康", "AKY")],
    "榆林": [("榆林", "ALY")],
    "格尔木": [("格尔木", "GRO")],
    "日喀则": [("日喀则", "RKO")],
    "香港": [("香港西九龙", "XJA")],
}


class Stations:
    """站点库：支持按车站名 / 城市名 / 拼音 / 首字母检索。"""

    def __init__(self) -> None:
        self.by_code: dict[str, dict] = {}
        self.by_name: dict[str, dict] = {}
        self.city_stations: dict[str, list[str]] = {}
        self.source = "fallback"
        self._load()

    # ---------- 载入 ----------
    def _load(self) -> None:
        data = self._from_cache()
        if data is None:
            data = self._from_remote()
        if data is None:
            data = self._from_fallback()
        self._index(data)

    def _from_cache(self) -> list[dict] | None:
        try:
            if not os.path.exists(STATION_CACHE):
                return None
            if time.time() - os.path.getmtime(STATION_CACHE) > STATION_CACHE_TTL:
                return None
            with open(STATION_CACHE, encoding="utf-8") as f:
                payload = json.load(f)
            rows = payload.get("stations")
            if rows:
                self.source = "cache"
                return rows
        except Exception:
            pass
        return None

    def _from_remote(self) -> list[dict] | None:
        try:
            req = urllib.request.Request(STATION_URL, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", "replace")
            rows = self._parse_station_js(text)
            if not rows:
                return None
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(STATION_CACHE, "w", encoding="utf-8") as f:
                json.dump({"updated": time.time(), "stations": rows}, f, ensure_ascii=False)
            self.source = "12306"
            return rows
        except Exception:
            return None

    @staticmethod
    def _parse_station_js(text: str) -> list[dict]:
        m = re.search(r"station_names\s*=\s*'(.*)'", text, re.S)
        body = m.group(1) if m else text
        rows: list[dict] = []
        for item in body.split("@"):
            parts = item.split("|")
            if len(parts) < 5:
                continue
            rows.append({
                "py": parts[0], "name": parts[1], "code": parts[2],
                "pinyin": parts[3], "abbr": parts[4],
            })
        return rows

    def _from_fallback(self) -> list[dict]:
        rows: list[dict] = []
        for city, items in FALLBACK_STATIONS.items():
            for name, code in items:
                rows.append({"py": "", "name": name, "code": code,
                             "pinyin": city, "abbr": "", "city": city})
        return rows

    # ---------- 索引 ----------
    def _index(self, rows: list[dict]) -> None:
        self._passenger_codes = self._load_passenger_codes()
        self._session_learned: set[str] = set()
        # 客运车站目录（gaotie.com.cn）：只收录办理客运的车站，
        # 用于① 剔除 12306 站名表里的货运/编组站 ② 按行政区划归并同城车站
        try:
            from . import passenger_catalog as _pc
            self._catalog = _pc.load()
            self._admin_city: dict[str, str] = {}
            for city, names in self._catalog.get("cities", {}).items():
                for n in names:
                    self._admin_city.setdefault(n, city)
            self.catalog_source = self._catalog.get("source", "")
        except Exception:
            self._catalog = {"available": False, "stations": set(), "cities": {}}
            self._admin_city = {}
            self.catalog_source = ""

        for row in rows:
            name = row.get("name") or ""
            code = (row.get("code") or "").upper()
            if not name or not code:
                continue
            city = row.get("city") or self._guess_city(name)
            # 行政区划归属优先（瓦房店/普兰店 属于 大连；广宁寺/旅顺 也属于 大连）
            admin = self._admin_city.get(name)
            entry = {
                "name": name, "code": code, "city": city,
                "admin_city": admin or "",
                "pinyin": (row.get("pinyin") or "").lower(),
                "abbr": (row.get("abbr") or "").lower(),
                "passenger": self._is_passenger(code, name),
                "confirmed": code in self._passenger_codes,
            }
            self.by_code[code] = entry
            self.by_name[name] = entry
            self.city_stations.setdefault(city, []).append(name)
            if admin and admin != city:
                self.city_stations.setdefault(admin, []).append(name)

    @staticmethod
    def _load_passenger_codes() -> set[str]:
        """载入"已确认办理客运"的车站电报码（由 tools/build_passenger_stations.py 生成）。

        未生成时为空集合，此时不做严格过滤，只按站名特征排除明显的货运设施。
        """
        try:
            if os.path.exists(PASSENGER_CACHE):
                with open(PASSENGER_CACHE, encoding="utf-8") as fh:
                    return set(json.load(fh).get("codes") or [])
        except Exception:
            pass
        return set()

    def _is_passenger(self, code: str, name: str) -> bool:
        """判断车站是否办理客运。

        依据（可靠性从高到低）：
          1) **客运车站目录**（gaotie.com.cn）：若目录可用，且该站在名的**同时**
             不在目录里 —— 说明它是 12306 站名表里混入的货运/编组站，直接排除
             （实测识别出 广州西、石牌、白云湖、双流西 等 28 个）。
          2) **实测名单**：车次里出现过的车站一定办理客运 —— 命中即确认。
          3) 站名特征：含"货场/编组/线路所"等字样直接排除。
          4) 无法确认时保留（宁可多显示，也不误删能坐车的站）。
        """
        if code in self._passenger_codes or code in self._session_learned:
            return True
        if any(kw in name for kw in NON_PASSENGER_KEYWORDS):
            return False
        cat = getattr(self, "_catalog", None)
        if cat and cat.get("available"):
            if name in (cat.get("blocked") or set()):
                return False               # 手工屏蔽名单
            if name not in cat["stations"]:
                return False               # 客运目录里没有 -> 货运/编组性质
            return True
        return True

    def is_confirmed_passenger(self, code: str) -> bool:
        """是否已被实测数据确认办理客运。"""
        c = (code or "").upper()
        return c in self._passenger_codes or c in self._session_learned


    def learn_passenger(self, codes) -> int:
        """在真实查询中学习到客运站（车次里出现过的电报码），持久化到缓存。"""
        added = 0
        for code in codes:
            c = (code or "").upper()
            if not c or c in self._passenger_codes:
                continue
            if c not in self.by_code:
                continue
            self._passenger_codes.add(c)
            self._session_learned.add(c)
            entry = self.by_code.get(c)
            if entry:
                entry["passenger"] = True
                entry["confirmed"] = True
            added += 1
        if added:
            self._save_passenger_codes()
        return added

    def _save_passenger_codes(self) -> None:
        """增量合并保存：先读回磁盘上已有的代码，再并入本进程学到的，避免相互覆盖。

        （采集脚本 tools/build_passenger_stations.py 与服务进程都会写这个文件，
          因此写之前必须合并，否则后写的一方会把对方的结果冲掉。）
        """
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            merged = set(self._passenger_codes)
            if os.path.exists(PASSENGER_CACHE):
                try:
                    with open(PASSENGER_CACHE, encoding="utf-8") as fh:
                        merged |= set(json.load(fh).get("codes") or [])
                except Exception:
                    pass
            self._passenger_codes = merged
            payload = {
                "updated": time.time(),
                "source": "12306 实测车次 + 运行中学习",
                "total": len(merged),
                "codes": sorted(merged),
            }
            tmp = PASSENGER_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, PASSENGER_CACHE)
        except Exception:
            pass

    def passenger_codes(self) -> set[str]:
        return set(self._passenger_codes)

    @staticmethod
    def _guess_city(name: str) -> str:
        """从站名推断所属城市：优先匹配内置城市表。"""
        for city in FALLBACK_STATIONS:
            if name.startswith(city):
                return city
        for suffix in ("东", "南", "西", "北", "虹桥", "朝阳", "丰台", "新", "总站"):
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[: -len(suffix)]
        return name

    # ---------- 查询 ----------
    def code_of(self, name_or_code: str) -> str | None:
        s = (name_or_code or "").strip()
        if not s:
            return None
        if s.upper() in self.by_code:
            return s.upper()
        if s in self.by_name:
            return self.by_name[s]["code"]
        # 城市名 -> 该城主要车站（优先同名站，其次按站名长度）
        names = self.stations_of_city(s)
        if names:
            if s in names:
                return self.by_name[s]["code"]
            for n in sorted(names, key=len):
                return self.by_name[n]["code"]
        # 模糊：站名包含输入（优先客运站）
        cands = [e for n, e in self.by_name.items() if s in n]
        for e in sorted(cands, key=lambda e: (not e.get("passenger"), len(e["name"]))):
            return e["code"]
        return None

    def stations_of_city(self, city: str, passenger_only: bool = True) -> list[str]:
        """该城市的车站列表（默认只返回可乘车的客运站）。

        排序优先级（越靠前越可能是常用客运站）：
          1) 与城市同名的车站（大连、上海…）
          2) 已由实测数据确认办理客运的车站
          3) 内置"主要车站"表里列出的车站（北京南、上海虹桥…）
          4) 其余未确认车站（如货运性质的 XX西/XX东），排在最后并标记"未确认"
        """
        city = (city or "").strip()
        names = self.city_stations.get(city)
        if not names and city in self.by_name:
            names = [city]
        if not names:
            return []
        out = [n for n in names
               if not passenger_only or self.by_name[n].get("passenger", True)]
        if not out:                       # 全部被判为非客运时，至少保留同名站
            out = [n for n in names if n == city] or names

        from .provider_12306 import MAJOR_STATIONS
        known_major = set(MAJOR_STATIONS.get(city, []))
        known_major |= {n for n, _ in FALLBACK_STATIONS.get(city, [])}

        def priority(name: str) -> tuple:
            e = self.by_name[name]
            return (
                name != city,                        # 同名站优先
                not self.is_confirmed_passenger(e["code"]),   # 已确认客运优先
                name not in known_major,             # 内置主要站优先
                len(name),
            )

        return sorted(out, key=priority)

    def name_of_code(self, code: str) -> str:
        e = self.by_code.get((code or "").upper())
        return e["name"] if e else code

    def city_of_code(self, code: str) -> str:
        e = self.by_code.get((code or "").upper())
        return e["city"] if e else code

    def suggest(self, query: str, limit: int = 12, grouped: bool = True) -> list[dict]:
        """车站联想。

        grouped=True 时返回"城市分组 + 子车站"结构，便于模糊搜索：
            [{"type":"city","city":"大连","code":"DFT","name":"大连",
              "stations":[{"name":"大连北","code":"DFT","passenger":true}, ...]}, ...]
        只有单个车站的城市仍返回 city 项，stations 里含该站本身。
        """
        q = (query or "").strip()
        if not q:
            return []
        ql = q.lower()
        hits: list[tuple[int, dict]] = []

        # 电报码精确命中优先（如 AOH -> 上海虹桥）
        exact = self.by_code.get(q.upper())
        if exact and len(q) == 3 and q.isalpha():
            hits.append((0, exact))

        def rank(entry: dict) -> int:
            name, city = entry["name"], entry["city"]
            if not entry.get("passenger", True):
                return 99                     # 非客运站不参与联想
            if name == q:
                return 0
            if city == q and name == q:
                return 0
            if name.startswith(q):
                return 1
            if city == q:
                return 2
            if city.startswith(q):
                return 3
            if entry["abbr"] == ql or entry["pinyin"] == ql:
                return 4
            if entry["abbr"].startswith(ql) or entry["pinyin"].startswith(ql):
                return 5
            if q in name or q in city:
                return 6
            return 99

        for entry in self.by_name.values():
            r = rank(entry)
            if r < 99 and (r, entry) not in hits:
                hits.append((r, entry))
        hits.sort(key=lambda h: (h[0], len(h[1]["name"])))

        if not grouped:
            out, seen = [], set()
            for _, e in hits:
                key = (e["name"], e["code"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": e["name"], "code": e["code"], "city": e["city"],
                            "passenger": e.get("passenger", True)})
                if len(out) >= limit:
                    break
            for o in out:
                o["peers"] = self._peer_stations(o["city"], o["name"])
            return out

        # ---- 分组：按城市聚合，主站排在前面 ----
        cities: dict[str, list[dict]] = {}
        order: list[str] = []
        for _, e in hits:
            city = e["city"]
            if city not in cities:
                cities[city] = []
                order.append(city)
            cities[city].append(e)

        out_groups: list[dict] = []
        for city in order:
            entries = cities[city]
            entries.sort(key=lambda e: (e["name"] != city, len(e["name"])))
            main = entries[0]
            stations = []
            for e in entries:
                if e["name"] in [s["name"] for s in stations]:
                    continue
                stations.append({"name": e["name"], "code": e["code"],
                                 "passenger": e.get("passenger", True),
                                 "confirmed": e.get("confirmed", False),
                                 "is_main": e["name"] == city})
            # 该城市还有其他客运站时一并补上（方便按城市一次选全）
            for n in self.stations_of_city(city):
                if n in [s["name"] for s in stations]:
                    continue
                e = self.by_name[n]
                stations.append({"name": n, "code": e["code"],
                                 "passenger": True,
                                 "confirmed": e.get("confirmed", False),
                                 "is_main": n == city})
            out_groups.append({
                "type": "city",
                "city": city,
                "name": city if city == main["name"] else main["name"],
                "code": main["code"],
                "stations": stations[:12],
                "station_count": len(stations),
            })
            if len(out_groups) >= limit:
                break
        return out_groups

    def _peer_stations(self, city: str, exclude: str) -> list[dict]:
        peers = self.stations_of_city(city)
        return [{"name": n, "code": self.by_name[n]["code"]}
                for n in peers if n != exclude][:4]


_singleton: Stations | None = None


def get_stations(refresh: bool = False) -> Stations:
    global _singleton
    if _singleton is None or refresh:
        _singleton = Stations()
    return _singleton


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    st = get_stations()
    print("source:", st.source, "| stations:", len(st.by_name), "| cities:", len(st.city_stations))
    for q in ["北京", "上海虹桥", "AOH", "bj", "广州"]:
        print(f"  {q} ->", st.suggest(q, 4))
