"""从火狐（Firefox）读取指定站点的 Cookie，供本程序携带登录态访问。

Firefox 的 Cookie 以明文存放在配置目录的 cookies.sqlite（moz_cookies 表），
因此无需解密即可使用，也不干扰你日常用火狐浏览。

安全说明：
  * 只读复制一份数据库到临时文件再读取，绝不修改火狐任何数据；
  * 只提取本程序需要出行的站点域（12306 / 携程 / 去哪儿 / 同程），不落地其他站点 Cookie；
  * Cookie 仅保存在内存与本地缓存文件中（cache/cookies.json，可随时删除）。
"""

from __future__ import annotations

import configparser
import glob
import http.cookiejar
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from .paths import cache_dir, cache_file

CACHE_DIR = cache_dir()
COOKIE_CACHE = cache_file("cookies.json")
COOKIE_TTL = 600            # 10 分钟内不重复读火狐数据库

# 本程序需要登录态的站点
TARGET_DOMAINS = ["12306.cn", "ctrip.com", "qunar.com", "ly.com", "trip.com", "133.cn"]

FF_ROAM = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")
FF_LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Mozilla", "Firefox")


def firefox_profiles() -> list[str]:
    """返回火狐所有配置目录（默认配置优先）。"""
    out: list[str] = []
    default: list[str] = []
    ini = os.path.join(FF_ROAM, "profiles.ini")
    if os.path.exists(ini):
        cfg = configparser.ConfigParser()
        try:
            cfg.read(ini, encoding="utf-8")
        except Exception:
            cfg = None
        if cfg:
            for sec in cfg.sections():
                if not sec.lower().startswith("profile"):
                    continue
                path = cfg.get(sec, "Path", fallback="")
                if not path:
                    continue
                is_rel = cfg.get(sec, "IsRelative", fallback="1") == "1"
                full = os.path.join(FF_ROAM, path) if is_rel else path
                if not os.path.isdir(full):
                    continue
                if cfg.get(sec, "Default", fallback="0") == "1":
                    default.append(full)
                else:
                    out.append(full)
    for base in (FF_ROAM, FF_LOCAL):
        for p in glob.glob(os.path.join(base, "Profiles", "*")):
            if os.path.isdir(p) and p not in default and p not in out:
                out.append(p)
    return default + out


def _read_cookies_from(db_path: str) -> list[dict]:
    """从 cookies.sqlite 读取目标站点 Cookie。

    注意：Firefox 的 expiry 字段是**毫秒**级 Unix 时间戳（不同版本可能是微秒），
    这里按量级自动判断，避免把有效 Cookie 误判为过期。
    """
    tmp = os.path.join(tempfile.gettempdir(), f"ffck_{os.getpid()}_{int(time.time()*1000)}.sqlite")
    rows: list[dict] = []
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        now_ms = int(time.time() * 1000)
        for dom in TARGET_DOMAINS:
            for r in con.execute(
                    "select host, name, value, path, expiry, isSecure, isHttpOnly "
                    "from moz_cookies where host like ?", (f"%{dom}%",)):
                host = r["host"] or ""
                # 精确域名匹配：避免 "trip.com" 误命中 "ctrip.com"
                bare = host.lstrip(".")
                if not (bare == dom or bare.endswith("." + dom)):
                    continue
                exp = r["expiry"] or 0
                if exp:
                    exp_ms = exp // 1000 if exp > 10 ** 14 else exp   # 微秒 -> 毫秒
                    if exp_ms < now_ms:                              # 已过期
                        continue
                    exp_s = exp_ms // 1000
                else:
                    exp_s = None                                     # 会话 Cookie
                rows.append({
                    "domain": host,
                    "name": r["name"],
                    "value": r["value"],
                    "path": r["path"] or "/",
                    "expiry": exp_s,
                    "secure": bool(r["isSecure"]),
                    "http_only": bool(r["isHttpOnly"]),
                })
        con.close()
    except Exception:
        return []
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return rows


def load_cookies(force: bool = False) -> dict:
    """读取火狐登录态。返回 {"cookies": [...], "by_domain": {...}, "profile": ..., "error": ...}"""
    if not force and os.path.exists(COOKIE_CACHE):
        try:
            if time.time() - os.path.getmtime(COOKIE_CACHE) < COOKIE_TTL:
                with open(COOKIE_CACHE, encoding="utf-8") as fh:
                    return json.load(fh)
        except Exception:
            pass

    profiles = firefox_profiles()
    best: dict = {"cookies": [], "profile": "", "error": ""}
    for prof in profiles:
        db = os.path.join(prof, "cookies.sqlite")
        if not os.path.exists(db):
            continue
        rows = _read_cookies_from(db)
        if len(rows) > len(best["cookies"]):
            best = {"cookies": rows, "profile": prof, "error": ""}
    if not best["cookies"]:
        best["error"] = "未在火狐中找到目标站点 Cookie（请在火狐里登录 12306 / 携程 / 去哪儿）"

    by_domain: dict[str, int] = {}
    for c in best["cookies"]:
        for dom in TARGET_DOMAINS:
            if dom in c["domain"]:
                by_domain[dom] = by_domain.get(dom, 0) + 1
    best["by_domain"] = by_domain
    best["updated"] = time.time()

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(COOKIE_CACHE, "w", encoding="utf-8") as fh:
            json.dump(best, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return best


def cookie_header(domain_key: str) -> str:
    """拼出某个站点可用的 Cookie 请求头。"""
    data = load_cookies()
    parts = []
    for c in data.get("cookies", []):
        if domain_key in c["domain"]:
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def cookiejar(domain_key: str) -> http.cookiejar.CookieJar:
    """构造 urllib 可用的 CookieJar（带正确 domain/path）。"""
    data = load_cookies()
    jar = http.cookiejar.CookieJar()
    for c in data.get("cookies", []):
        if domain_key not in c["domain"]:
            continue
        host = c["domain"].lstrip(".")
        domain_specified = c["domain"].startswith(".")
        try:
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"],
                port=None, port_specified=False,
                domain=c["domain"], domain_specified=domain_specified,
                domain_initial_dot=domain_specified,
                path=c["path"], path_specified=True,
                secure=c["secure"], expires=c["expiry"],
                discard=False, comment=None, comment_url=None,
                rest={}, rfc2109=False))
        except Exception:
            continue
    return jar


def summary() -> dict:
    data = load_cookies()
    return {
        "profile": os.path.basename(data.get("profile") or ""),
        "total": len(data.get("cookies", [])),
        "by_domain": data.get("by_domain", {}),
        "updated": data.get("updated"),
        "error": data.get("error", ""),
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = summary()
    print("火狐登录态：")
    print("  配置目录:", s["profile"])
    print("  目标站点 Cookie 总数:", s["total"])
    for dom, n in sorted(s["by_domain"].items()):
        print(f"    {dom:<14s} {n:>3d} 条")
    if s["error"]:
        print("  提示:", s["error"])
    for dom in ("ctrip.com", "qunar.com"):
        h = cookie_header(dom)
        print(f"\n  {dom} Cookie 头长度: {len(h)}")
        print("    ", (h[:160] + "...") if len(h) > 160 else h)
