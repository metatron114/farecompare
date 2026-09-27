"""比价搜索服务（Python 标准库，无第三方依赖）。

启动： python app/server.py [--port 8765] [--no-browser]
接口：
  GET  /                        前端页面
  GET  /static/*                静态资源
  GET  /api/suggest?q=北京       车站/城市联想
  POST /api/search              发起搜索（异步任务，返回 task_id）
  GET  /api/progress?task_id=   查询进度
  GET  /api/result?task_id=     取结果
  GET  /api/health              数据源健康状态
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .compare import build_options, comparison_table, filter_options, rank, summarize
from .planner import JourneyPlanner
from .provider_12306 import Ticket12306
from .provider_flight import provider_status
from .stations import get_stations

from .paths import WEB_DIR

CACHE_TTL = 600
# 单次搜索允许的 12306 请求上限：超出即提前收尾，保证响应速度可预期
DEFAULT_REQUEST_BUDGET = 35
_cache: dict[str, tuple[float, dict]] = {}
_range_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
_client = Ticket12306()


def _range_key(dep: str, arr: str, start: str, end: str) -> str:
    return f"{dep}|{arr}|{start}|{end}"


# --------------------------------------------------------------------------
# 搜索任务
# --------------------------------------------------------------------------
def _cache_key(dep: str, arr: str, date: str, allow_transfer: bool,
               allow_air_rail: bool = False, exclude_no_baggage: bool = True) -> str:
    """缓存"全量候选方案"，与排序/筛选无关，因此键中不含筛选参数。"""
    return (f"{dep}|{arr}|{date}|{int(allow_transfer)}"
            f"|{int(allow_air_rail)}|{int(exclude_no_baggage)}")


def _present(all_options: list[dict], params: dict, meta: dict) -> dict:
    """对全量候选方案应用筛选、排序、汇总。每次请求独立计算，缓存只存全量。"""
    filtered = filter_options(
        all_options,
        modes=params.get("modes") or None,
        only_direct=params.get("only_direct", False),
        budget=params.get("budget"),
        depart_from=params.get("depart_from"),
        depart_to=params.get("depart_to"),
    )
    ordered = rank(filtered, sort=params.get("sort", "price"), limit=60)
    errors = list(meta.get("errors", []))
    budget_used = meta.get("budget_used")
    budget_limit = meta.get("budget_limit")
    if budget_limit and budget_used is not None and budget_used >= budget_limit:
        errors.append(f"本次搜索已达 12306 请求上限（{budget_limit} 次），"
                      f"为保证响应速度提前结束；可用 --budget 调大上限重试")
    return {
        "query": meta["query"],
        "options": ordered,
        "summary": summarize(ordered),
        "comparison": comparison_table(ordered),
        "all_count": len(all_options),
        "flight_note": meta.get("flight_note", ""),
        "flight_source": meta.get("flight_source", ""),
        "flight_provider": meta.get("flight_provider", ""),
        "flight_live": meta.get("flight_live", False),
        "excluded_no_baggage": meta.get("excluded_no_baggage", []),
        "flight_calendar": meta.get("flight_calendar", []),
        "train_note": meta.get("train_note", ""),
        "train_sources": meta.get("train_sources", {}),
        "gaotie_note": meta.get("gaotie_note", ""),
        "links": meta.get("links", {}),
        "errors": errors[:5],
        "requests": meta.get("requests", 0),
        "budget_used": budget_used,
        "budget_limit": budget_limit,
        "elapsed": meta.get("elapsed", 0),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _run_search(task_id: str, params: dict) -> None:
    task = _tasks[task_id]
    dep = params["from"]
    arr = params["to"]
    date = params["date"]
    allow_transfer = params.get("allow_transfer", True)
    include_train = params.get("include_train", True)
    include_flight = params.get("include_flight", True)
    allow_air_rail = params.get("allow_air_rail", False)
    exclude_no_baggage = params.get("exclude_no_baggage", True)

    key = _cache_key(dep, arr, date, allow_transfer, allow_air_rail, exclude_no_baggage)
    use_cache = not params.get("no_cache")
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            meta = dict(hit[1])
            meta["elapsed"] = 0
            task.update({"status": "done", "progress": 100, "message": "命中缓存",
                         "result": _present(meta["all_options"], params, meta),
                         "cached": True})
            return

    def progress(msg: str, pct: int) -> None:
        task["message"] = msg
        task["progress"] = max(task.get("progress", 0), min(99, int(pct)))

    try:
        started = time.time()
        before = _client.requests
        # 单次搜索的请求预算 + 区间/票价去重缓存，保证响应速度可控
        _client.begin_budget(int(params.get("request_budget", DEFAULT_REQUEST_BUDGET)))
        planner = JourneyPlanner(client=_client, progress=progress)
        progress("查询直达车次与票价", 3)
        direct = planner.direct(dep, arr, date,
                                include_train=include_train,
                                include_flight=include_flight,
                                exclude_no_baggage=exclude_no_baggage)
        transfers: list[dict] = []
        if allow_transfer and include_train:
            # 把直达的"最低价 / 最短耗时"传给中转，用于剪枝与质量门
            d_prices = [t["price"] for t in direct.get("trains", [])]
            d_durs = [t["duration_min"] for t in direct.get("trains", []) if t["duration_min"]]
            # 直达已有方案时，中转只分到剩余预算的一部分（约 45%），避免它吃掉全部配额
            used_now = _client.budget_used
            extra = 14 if d_prices else max(14, _client.budget_left() - 2)
            _client.set_soft_limit(used_now, extra)
            transfers = planner.transfer(
                dep, arr, date, max_options=8, base_pct=38, span_pct=40,
                direct_best=min(d_prices) if d_prices else None,
                direct_fastest=min(d_durs) if d_durs else None)

        air_rail_opts: list[dict] = []
        if allow_air_rail and include_train and include_flight:
            _client.clear_soft_limit()
            air_rail_opts = planner.air_rail(dep, arr, date, hubs=3, max_options=4)

        progress("汇总比价", 97)

        all_options = build_options(direct, transfers, dep, arr, air_rail_opts)
        meta = {
            "query": {"from": dep, "to": arr, "date": date, "weekday": _weekday(date)},
            "all_options": all_options,
            "flight_note": direct.get("flight_note", ""),
            "flight_source": direct.get("flight_source", ""),
            "flight_provider": direct.get("flight_provider", ""),
            "flight_live": direct.get("flight_live", False),
            "excluded_no_baggage": direct.get("excluded_no_baggage", []),
            "flight_calendar": direct.get("flight_calendar", []),
            "train_note": direct.get("train_note", ""),
            "train_sources": direct.get("train_sources", {}),
            "gaotie_note": direct.get("gaotie_note", ""),
            "links": direct.get("links", {}),
            "errors": planner.errors[:5],
            "requests": _client.requests - before,
            "budget_used": _client.budget_used,
            "budget_limit": _client.budget_limit,
            "elapsed": round(time.time() - started, 1),
        }
        with _cache_lock:
            _cache[key] = (time.time(), meta)
        task.update({"status": "done", "progress": 100, "message": "完成",
                     "result": _present(all_options, params, meta)})
    except Exception as exc:
        traceback.print_exc()
        task.update({"status": "error", "progress": 100, "message": str(exc),
                     "result": None})



def _calendar_lows(dep: str, arr: str, start: str, end: str,
                   include_train: bool, train_days: int = 0) -> dict:
    """日期区间每日最低价。

    机票：同程列表页自带的未来一个月价格日历（一次抓取覆盖整段区间，约 7 秒）
    火车：12306 逐日查询当日该区间最低车票。受源站限流影响，默认只抽样少量天数
          （优先抽机票最便宜的几天，最有比较价值），0 表示不查火车。
    """
    from .provider_flight import search_range as flight_range

    fres = flight_range(dep, arr, start, end)
    days: dict[str, dict] = {}
    for d in fres.get("days", []):
        days[d["date"]] = {"date": d["date"], "weekday": d["weekday"],
                           "flight_min": d.get("price"), "train_min": None}

    notes = [fres.get("note", "")]
    if include_train and train_days > 0:
        stat = get_stations()
        dep_code = stat.code_of(dep)
        arr_code = stat.code_of(arr)
        if dep_code and arr_code:
            try:
                d0 = datetime.date.fromisoformat(start)
                d1 = datetime.date.fromisoformat(end)
            except Exception:
                d0 = d1 = datetime.date.today()
            probe_days = min(train_days, (d1 - d0).days + 1)
            ordered_days = sorted(days.values(),
                                  key=lambda x: (x["flight_min"] is None, x["flight_min"] or 0))
            picked = [x["date"] for x in ordered_days[:probe_days]]
            ok = 0
            for day in picked:
                try:
                    rows = _client.query_tickets(dep_code, arr_code, day)
                except Exception:
                    continue
                if not rows:
                    continue
                priced = _client.query_price_many(
                    sorted(rows, key=lambda r: r["depart"])[:3], limit=3)
                best = None
                for rec in priced:
                    for p in (rec.get("prices") or []):
                        if p.get("stand"):
                            continue
                        if best is None or p["price"] < best:
                            best = p["price"]
                if best is not None:
                    entry = days.setdefault(day, {"date": day, "weekday": _weekday(day),
                                                  "flight_min": None, "train_min": None})
                    entry["train_min"] = best
                    ok += 1
            notes.append(f"火车已抽样 {ok}/{len(picked)} 天")

    out = sorted(days.values(), key=lambda x: x["date"])
    for d in out:
        lows = [v for v in (d["flight_min"], d["train_min"]) if v]
        d["min"] = min(lows) if lows else None
        d["best_mode"] = ("飞机" if d["flight_min"] and
                          (not d["train_min"] or d["flight_min"] <= d["train_min"]) else "火车") \
            if lows else ""
    cheapest = min((d for d in out if d["min"]), key=lambda x: x["min"], default=None)
    return {"days": out, "cheapest": cheapest, "source": fres.get("source"),
            "provider": fres.get("provider"), "range": {"start": start, "end": end},
            "note": "；".join(n for n in notes if n)}



def _weekday(date: str) -> str:
    try:
        d = datetime.date.fromisoformat(date)
        return "周" + "一二三四五六日"[d.weekday()]
    except Exception:
        return ""


def _validate_range(p: dict) -> str | None:
    if not p.get("from") or not p.get("to"):
        return "请填写出发地与目的地"
    if p["from"] == p["to"]:
        return "出发地与目的地不能相同"
    start, end = p.get("start") or "", p.get("end") or ""
    try:
        d0 = datetime.date.fromisoformat(start)
        d1 = datetime.date.fromisoformat(end)
    except Exception:
        return "日期格式应为 YYYY-MM-DD"
    if d1 < d0:
        return "结束日期不能早于开始日期"
    today = datetime.date.today()
    if d0 < today:
        return "开始日期不能早于今天"
    if (d1 - d0).days > 60:
        return "区间最长 60 天"
    return None


def start_range(params: dict) -> str:
    task_id = f"r{int(time.time() * 1000)}{os.getpid() % 1000}"
    with _tasks_lock:
        _tasks[task_id] = {"id": task_id, "status": "running", "progress": 0,
                           "message": "准备中", "started": time.time(),
                           "result": None, "params": params}
    _pool.submit(_run_range, task_id, params)
    return task_id


def _run_range(task_id: str, params: dict) -> None:
    task = _tasks[task_id]
    dep, arr = params["from"], params["to"]
    start, end = params["start"], params["end"]
    include_train = bool(params.get("include_train", True))

    def progress(msg: str, pct: int) -> None:
        task["message"] = msg
        task["progress"] = max(task.get("progress", 0), min(99, int(pct)))

    try:
        rkey = _range_key(dep, arr, start, end)
        if not params.get("no_cache"):
            with _cache_lock:
                hit = _range_cache.get(rkey)
            if hit and time.time() - hit[0] < CACHE_TTL:
                task.update({"status": "done", "progress": 100, "message": "命中缓存",
                             "result": hit[1], "cached": True})
                return
        progress("读取机票价格日历", 10)
        calendar = _calendar_lows(dep, arr, start, end, include_train,
                                  train_days=int(params.get("train_days", 0)))
        progress("完成", 100)
        result = {"query": {"from": dep, "to": arr, "start": start, "end": end},
                  **calendar,
                  "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        # 空结果不缓存，避免一次抓取失败把后续查询也变成空
        if result.get("days"):
            with _cache_lock:
                _range_cache[_range_key(dep, arr, start, end)] = (time.time(), result)
        task.update({"status": "done", "progress": 100, "message": "完成", "result": result})
    except Exception as exc:
        traceback.print_exc()
        task.update({"status": "error", "progress": 100, "message": str(exc), "result": None})


def start_search(params: dict) -> str:
    task_id = f"t{int(time.time() * 1000)}{os.getpid() % 1000}"
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "progress": 0,
            "message": "准备中",
            "started": time.time(),
            "result": None,
            "params": params,
        }
        # 清理过期任务
        for k in [k for k, v in _tasks.items() if time.time() - v["started"] > 3600]:
            _tasks.pop(k, None)
    _pool.submit(_run_search, task_id, params)
    return task_id


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FareCompare/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，仅保留错误
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("  HTTP %s %s\n" % (args[1], args[0]))

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._file(os.path.join(WEB_DIR, "index.html"))
        if route.startswith("/static/"):
            rel = route[len("/static/"):].replace("..", "")
            return self._file(os.path.join(WEB_DIR, "static", rel))

        if route == "/api/suggest":
            q = (qs.get("q") or [""])[0]
            try:
                limit = max(1, min(30, int((qs.get("limit") or ["12"])[0])))
            except Exception:
                limit = 12
            grouped = (qs.get("grouped") or ["1"])[0] not in ("0", "false")
            stations = get_stations()
            return self._json({"source": stations.source,
                               "passenger_known": len(stations.passenger_codes()),
                               "items": stations.suggest(q, limit, grouped=grouped)})

        if route == "/api/stations":
            # 城市 -> 客运车站子分组（供前端"按城市选全部车站"使用）
            city = (qs.get("city") or [""])[0].strip()
            stations = get_stations()
            if city:
                names = stations.stations_of_city(city)
                return self._json({
                    "city": city,
                    "stations": [{"name": n, "code": stations.by_name[n]["code"],
                                  "passenger": stations.by_name[n].get("passenger", True)}
                                 for n in names],
                })
            return self._json({"cities": len(stations.city_stations),
                               "stations": len(stations.by_name),
                               "passenger_known": len(stations.passenger_codes())})

        if route == "/api/progress":
            tid = (qs.get("task_id") or [""])[0]
            task = _tasks.get(tid)
            if not task:
                return self._json({"error": "任务不存在"}, 404)
            return self._json({k: task[k] for k in ("id", "status", "progress", "message")})

        if route == "/api/result":
            tid = (qs.get("task_id") or [""])[0]
            task = _tasks.get(tid)
            if not task:
                return self._json({"error": "任务不存在"}, 404)
            return self._json({"status": task["status"], "message": task["message"],
                               "result": task.get("result")})

        if route == "/api/health":
            return self._json(_health())

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "请求体不是合法 JSON"}, 400)

        if parsed.path == "/api/search":
            err = _validate(payload)
            if err:
                return self._json({"error": err}, 400)
            task_id = start_search(payload)
            return self._json({"task_id": task_id})

        if parsed.path == "/api/range":
            err = _validate_range(payload)
            if err:
                return self._json({"error": err}, 400)
            task_id = start_range(payload)
            return self._json({"task_id": task_id})

        return self._json({"error": "not found"}, 404)


def _validate(p: dict) -> str | None:
    if not p.get("from") or not p.get("to"):
        return "请填写出发地与目的地"
    if p["from"] == p["to"]:
        return "出发地与目的地不能相同"
    date = p.get("date") or ""
    try:
        d = datetime.date.fromisoformat(date)
    except Exception:
        return "日期格式应为 YYYY-MM-DD"
    today = datetime.date.today()
    if d < today:
        return "日期不能早于今天"
    if (d - today).days > 60:
        return "12306 仅支持预售期内（约 60 天内）查询"
    if not p.get("include_train") and not p.get("include_flight"):
        return "请至少选择一种交通方式"
    return None


def _gaotie_status() -> dict:
    """gaotie 火车票源状态（主搜索通道，无限流）。"""
    try:
        from . import provider_gaotie as pg
        st = pg.stats()
        return {
            "name": "gaotie.com.cn 高铁网",
            "live": True,
            "reachable": True,
            **st,
            "note": "主搜索通道：两站间全部车次 + 公布票价，无频率限制；实时折扣以 12306 为准",
        }
    except Exception as exc:
        return {"name": "gaotie.com.cn 高铁网", "reachable": False,
                "error": str(exc)[:80]}


def _health() -> dict:
    stations = get_stations()
    from .provider_flight import browser_backend
    try:
        from . import passenger_catalog as pc
        catalog = pc.stats()
        catalog["blocked"] = len(pc.load_blocklist())
    except Exception as exc:
        catalog = {"available": False, "error": str(exc)[:80]}
        pc = None
    # 统计被剔除的货运/编组站数量
    excluded = 0
    if pc is not None and catalog.get("available"):
        excluded = sum(1 for e in stations.by_name.values() if not e.get("passenger", True))
    return {
        "stations": {"source": stations.source, "count": len(stations.by_name),
                     "cities": len(stations.city_stations),
                     "live": stations.source in ("12306", "cache")},
        "passenger_catalog": {**catalog, "excluded_stations": excluded},
        "train_12306": {
            "name": "12306 火车票",
            "live": True,
            "note": "实时余票 + 真实票价（含中转方案）；作为 gaotie 的补充与实时折扣来源",
            "requests_made": _client.requests,
            "cooldowns": _client.cooldowns,
        },
        "train_gaotie": _gaotie_status(),
        "flights": provider_status(),
        "browser": browser_backend(),
        "train_requests": _client.requests,
        "train_cooldowns": _client.cooldowns,
        "request_budget": DEFAULT_REQUEST_BUDGET,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _port_in_use(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def _wait_ready(host: str, port: int, timeout: float = 8.0) -> bool:
    """确认服务真的能响应，再打开浏览器（避免"启动了但页面打不开"）。"""
    deadline = time.time() + timeout
    url = f"http://{host}:{port}/api/suggest?q=bj"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
        budget: int | None = None, quiet: bool = False) -> int:
    """启动服务（供 CLI 调用）。返回进程退出码。

    * 端口被本服务占用 -> 直接打开已有页面
    * 端口被其他程序占用 -> 返回 1 并提示换端口
    * open_browser=True 时先探活再打开浏览器，避免"启动了但页面打不开"
    """
    global DEFAULT_REQUEST_BUDGET
    if budget:
        DEFAULT_REQUEST_BUDGET = budget

    url = f"http://{host}:{port}/"
    if _port_in_use(host, port):
        if _is_our_service(host, port):
            print(f"检测到本服务已在运行：{url}")
            if open_browser:
                webbrowser.open(url)
            return 0
        print(f"[错误] 端口 {port} 已被其他程序占用。")
        print(f"       请换端口启动，例如： farecompare serve --port {port + 1}")
        return 1

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"[错误] 无法监听 {host}:{port} —— {exc}")
        print(f"       请换端口启动，例如： farecompare serve --port {port + 1}")
        return 1

    if not quiet:
        from . import __version__
        print("=" * 66)
        print(f"  车票 / 机票 比价搜索  v{__version__}")
        print("=" * 66)
        print(f"  界面地址： {url}")
        print("  火车数据： gaotie.com.cn（车次通道，无限流） + 12306（实时票价）")
        print("  机票数据： 同程旅行实时报价；携程/去哪儿可达时自动启用")
        print(f"  请求预算： 单次搜索最多 {DEFAULT_REQUEST_BUDGET} 次 12306 请求")
        print("  停止服务： Ctrl + C")
        print("=" * 66)

    def open_when_ready() -> None:
        if _wait_ready(host, port):
            try:
                webbrowser.open(url)
            except Exception:
                print(f"  请手动打开：{url}")
        else:
            print(f"  [注意] 服务未能在预期时间内响应，请手动打开：{url}")

    if open_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def _is_our_service(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        return "train_12306" in body and "flights" in body
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    """兼容入口：`python -m farecompare.server --port 8765`。"""
    import argparse
    ap = argparse.ArgumentParser(description="车票/机票比价搜索服务")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    args = ap.parse_args(argv)
    return run(host=args.host, port=args.port, open_browser=not args.no_browser,
               budget=args.budget)


if __name__ == "__main__":
    raise SystemExit(main())
