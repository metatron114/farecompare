"""命令行入口。

用法：
    farecompare                      # 启动本地服务并打开浏览器（默认端口 8765）
    farecompare serve --port 8080    # 指定端口
    farecompare serve --no-browser   # 不自动打开浏览器
    farecompare fetch stations       # 更新铁路车站库
    farecompare fetch airports       # 更新民航城市库（需要本机 Edge）
    farecompare fetch gaotie         # 更新客运车站目录（过滤货运站）
    farecompare fetch all            # 全部更新
    farecompare check                # 环境与数据源自检
    farecompare version              # 版本信息
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from . import __version__


def _setup_console() -> None:
    """Windows 控制台默认是 GBK，打印 ¥ 等字符会抛 UnicodeEncodeError。

    这里尽量把标准输出切到 UTF-8；失败则降级为"无法编码的字符替换掉"，
    保证任何环境下都不会因为一个符号导致程序崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def _safe(text: str) -> str:
    """把文本转成当前控制台能输出的形式（最后的保险）。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except Exception:
        return text.encode(enc, "replace").decode(enc, "replace")



def _print_banner(port: int, host: str, budget: int, browser: str) -> None:
    print("=" * 66)
    print("  车票 · 机票 比价搜索  v" + __version__)
    print("=" * 66)
    print(f"  界面地址： http://{host}:{port}/")
    print("  火车数据： gaotie.com.cn（车次通道，无限流） + 12306（实时票价）")
    print("  机票数据： 同程旅行实时报价；携程/去哪儿可达时自动启用")
    print(f"  抓取浏览器：{browser}")
    print(f"  请求预算： 单次搜索最多 {budget} 次 12306 请求")
    print("  停止服务： Ctrl + C")
    print("=" * 66)


def _browser_name() -> str:
    try:
        from .provider_flight import browser_backend
        info = browser_backend()
        return info.get("name") or "无"
    except Exception:
        return "无（机票将降级为参考价）"


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server as srv

    srv.DEFAULT_REQUEST_BUDGET = args.budget

    url = f"http://{args.host}:{args.port}/"
    if srv._port_in_use(args.host, args.port):
        if srv._is_our_service(args.host, args.port):
            print(f"检测到本服务已在运行：{url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"[错误] 端口 {args.port} 已被其他程序占用。")
        print(f"       请换端口，例如： farecompare serve --port {args.port + 1}")
        return 1

    try:
        httpd = srv.ThreadingHTTPServer((args.host, args.port), srv.Handler)
    except OSError as exc:
        print(f"[错误] 无法监听 {args.host}:{args.port} —— {exc}")
        return 1

    _print_banner(args.port, args.host, args.budget, _browser_name())

    if not args.no_browser:
        def open_when_ready() -> None:
            if srv._wait_ready(args.host, args.port):
                try:
                    webbrowser.open(url)
                except Exception:
                    print(f"  请手动打开：{url}")
            else:
                print(f"  [注意] 服务未能及时响应，请手动打开：{url}")

        threading.Thread(target=open_when_ready, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """环境与数据源自检（不需要先启动服务）。"""
    from .paths import cache_dir
    from .stations import get_stations
    from . import china_geo, passenger_catalog, ly_cities, airports
    from .provider_gaotie import stats as gaotie_stats

    print("=" * 66)
    print("  环境与数据源检查  v" + __version__)
    print("=" * 66)
    print(f"  Python      : {sys.version.split()[0]} ({sys.platform})")
    print(f"  缓存目录    : {cache_dir()}")

    st = get_stations()
    print(f"  铁路车站库  : {len(st.by_name)} 个车站 / {len(st.city_stations)} 个城市"
          f"（来源 {st.source}）")
    cat = passenger_catalog.stats()
    excluded = sum(1 for e in st.by_name.values() if not e.get("passenger", True))
    print(f"  客运车站目录: {cat['stations']} 个客运站，已剔除货运站 {excluded} 个"
          f"（可用={cat['available']}）")
    geo = china_geo.describe()
    print(f"  城市规划数据: {geo['provinces']} 个省级区划 / {geo['hubs']} 个枢纽")
    print(f"  民航城市库  : {len(ly_cities.all_cities())} 个城市 / "
          f"{len(ly_cities.all_airports())} 个机场")
    g = gaotie_stats()
    print(f"  gaotie 通道 : {g['slugs']} 个车站拼音标识（缓存线路 {g['cached_routes']} 条）")
    print(f"  抓取浏览器  : {_browser_name()}")

    # 快速连通性检查
    print("\n  连通性：")
    for name, fn in (("gaotie 车次", _probe_gaotie), ("同程机票", _probe_ly)):
        try:
            print(_safe(f"    {name}: {fn()}"))
        except Exception as exc:
            print(_safe(f"    {name}: 失败 {type(exc).__name__}: {str(exc)[:70]}"))
    try:
        from .provider_flight import provider_status
        for k, v in provider_status().items():
            flag = "可用" if v.get("live_data") else "不可用（保留接口）"
            print(f"    {k}: {flag}")
    except Exception as exc:
        print(f"    机票源状态检查失败: {exc}")
    print("=" * 66)
    return 0


def _probe_gaotie() -> str:
    from .provider_gaotie import route_trains
    r = route_trains("上海", "北京")
    if not r["trains"]:
        return r.get("error") or "无数据"
    t = r["trains"][0]
    return f"上海→北京 {len(r['trains'])} 趟，首趟 {t['code']} ¥{t['price']:.0f}"


def _probe_ly() -> str:
    import datetime
    from .provider_flight import search_flights
    day = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
    r = search_flights("北京", "上海", day, limit=3)
    return f"来源 {r['source']}，{len(r['flights'])} 个航班"


def cmd_fetch(args: argparse.Namespace) -> int:
    """更新各类基础数据（在主进程内直接执行，不依赖外部脚本路径）。"""
    from .fetch import fetch_stations, fetch_airports, fetch_gaotie_catalog, build_city_map

    targets = args.target
    if "all" in targets:
        targets = ["stations", "gaotie", "airports", "citymap"]
    rc = 0
    for t in targets:
        try:
            if t == "stations":
                fetch_stations()
            elif t == "gaotie":
                fetch_gaotie_catalog(with_cities=args.cities, workers=args.workers)
            elif t == "airports":
                fetch_airports()
            elif t == "citymap":
                build_city_map()
            else:
                print(f"未知目标: {t}")
                rc = 2
        except Exception as exc:
            print(f"[失败] {t}: {type(exc).__name__}: {exc}")
            rc = 1
    return rc


def cmd_version(args: argparse.Namespace) -> int:
    print(f"farecompare {__version__}")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="farecompare",
        description="车票 / 机票 比价搜索：本地运行，按最低价排序",
    )
    ap.add_argument("-V", "--version", action="version",
                    version=f"farecompare {__version__}")
    sub = ap.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动本地服务（默认命令）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("FARECOMPARE_PORT", 8765)))
    p_serve.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_serve.add_argument("--budget", type=int, default=35,
                         help="单次搜索允许的 12306 请求上限（默认 35）")
    p_serve.set_defaults(func=cmd_serve)

    p_fetch = sub.add_parser("fetch", help="更新基础数据（车站库/客运目录/民航城市）")
    p_fetch.add_argument("target", nargs="+",
                         choices=["stations", "gaotie", "airports", "citymap", "all"],
                         help="要更新的数据")
    p_fetch.add_argument("--cities", action="store_true",
                         help="抓取每个城市页以补齐完整车站列表（较慢）")
    p_fetch.add_argument("--workers", type=int, default=6, help="并发数")
    p_fetch.set_defaults(func=cmd_fetch)

    p_check = sub.add_parser("check", help="环境与数据源自检")
    p_check.set_defaults(func=cmd_check)

    p_ver = sub.add_parser("version", help="显示版本")
    p_ver.set_defaults(func=cmd_version)
    return ap


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    # 无子命令时默认启动服务；同时兼容 `farecompare --port 8080`
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return 0
    return args.func(args)
