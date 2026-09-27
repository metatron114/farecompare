"""扫描火狐（Firefox）配置目录：登录态、Cookie 数量、是否为默认浏览器。

Firefox 把 Cookie 存在 cookies.sqlite（moz_cookies 表），
登录态数据（书签/密码）分别在 places.sqlite / logins.json。
只读复制，不修改任何浏览器数据。
"""
import configparser
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGETS = ["12306", "qunar", "ctrip", "trip.com", "ly.com", "133.cn", "variflight"]

APPDATA = os.environ.get("APPDATA", "")
FF_ROAM = os.path.join(APPDATA, "Mozilla", "Firefox")
FF_LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Mozilla", "Firefox")
FF_EXE = [r"C:\Program Files\Mozilla Firefox\firefox.exe",
          r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"]

print("=" * 78)
print("火狐浏览器检查")
print("=" * 78)
exe = next((p for p in FF_EXE if os.path.exists(p)), None)
print("  安装路径:", exe or "未找到")
if exe:
    try:
        import subprocess
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        print("  版本    :", (out.stdout or out.stderr).strip())
    except Exception as exc:
        print("  版本    : 获取失败", exc)

ini = os.path.join(FF_ROAM, "profiles.ini")
print("\n  profiles.ini:", ini, "存在" if os.path.exists(ini) else "不存在")

profiles: list[tuple[str, str]] = []
if os.path.exists(ini):
    cfg = configparser.ConfigParser()
    cfg.read(ini, encoding="utf-8")
    for sec in cfg.sections():
        if not sec.lower().startswith("profile"):
            continue
        name = cfg.get(sec, "Name", fallback="")
        path = cfg.get(sec, "Path", fallback="")
        is_rel = cfg.get(sec, "IsRelative", fallback="1") == "1"
        default = cfg.get(sec, "Default", fallback="0") == "1"
        full = os.path.join(FF_ROAM, path) if is_rel else path
        profiles.append((f"{name}{' [默认]' if default else ''}", full))

if not profiles:
    for p in glob.glob(os.path.join(FF_ROAM, "Profiles", "*")):
        profiles.append((os.path.basename(p), p))

for name, path in profiles:
    print(f"\n  ── 配置 {name}")
    print(f"     路径: {path}")
    print(f"     存在: {os.path.isdir(path)}")
    if not os.path.isdir(path):
        continue
    ck = os.path.join(path, "cookies.sqlite")
    if not os.path.exists(ck):
        print("     cookies.sqlite: 不存在（可能从未启动过该配置）")
        continue
    tmp = os.path.join(tempfile.gettempdir(), f"ff_{os.getpid()}_{int(time.time())}.sqlite")
    try:
        shutil.copy2(ck, tmp)
        con = sqlite3.connect(tmp)
        total = con.execute("select count(*) from moz_cookies").fetchone()[0]
        hosts = con.execute(
            "select host, count(*), max(lastAccessed) from moz_cookies group by host "
            "order by 2 desc limit 25").fetchall()
        print(f"     Cookie 总数: {total}")
        hits = [(h, n, la) for h, n, la in hosts
                if any(t in h for t in TARGETS)]
        if hits:
            print("     ★ 找到目标站点登录态：")
            for h, n, la in hits:
                ts = datetime.fromtimestamp(la / 1_000_000).strftime("%Y-%m-%d %H:%M") if la else "-"
                print(f"        {h:<40s} {n:>3d} 条  最近 {ts}")
        else:
            print("     未找到目标站点 Cookie，主要域名：")
            for h, n, la in hosts[:10]:
                print(f"        {h:<40s} {n:>3d} 条")
        con.close()
    except Exception as exc:
        print("     读取失败:", exc)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    for extra in ("logins.json", "places.sqlite", "prefs.js"):
        p = os.path.join(path, extra)
        if os.path.exists(p):
            print(f"     {extra}: {os.path.getsize(p)//1024} KB")
