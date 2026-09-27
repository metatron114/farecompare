"""统一路径管理：缓存目录、数据文件、网页资源。

缓存目录优先级：
  1) 环境变量 FARECOMPARE_HOME 指定的目录
  2) 项目根目录下的 cache/（便携模式：放在项目里，方便拷贝与版本管理）
  3) 用户目录 ~/.farecompare/cache（只读安装时的兜底）

这样既能"解压即用"，也能在 pip 安装到只读位置时正常工作。
"""

from __future__ import annotations

import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PACKAGE_DIR, "web")

# 项目根目录（包的上一级）；pip 安装时为 site-packages 的上一级，不用于写数据
_ROOT = os.path.dirname(PACKAGE_DIR)


def _env_home() -> str | None:
    v = os.environ.get("FARECOMPARE_HOME")
    return os.path.abspath(v) if v else None


def _portable_cache() -> str:
    """项目根目录下的 cache/（若可写则用它与源码放在一起，便于打包分发）。"""
    return os.path.join(_ROOT, "cache")


def _user_cache() -> str:
    base = (os.environ.get("XDG_CACHE_HOME")
            or (os.path.join(os.environ.get("LOCALAPPDATA", ""), "FareCompare")
                if os.name == "nt" else None)
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "farecompare")


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".writable")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def cache_dir() -> str:
    """返回可写的缓存目录（自动创建）。"""
    override = _env_home()
    if override:
        d = os.path.join(override, "cache") if not override.endswith("cache") else override
        os.makedirs(d, exist_ok=True)
        return d
    d = _portable_cache()
    if _writable(d):
        return d
    d = _user_cache()
    os.makedirs(d, exist_ok=True)
    return d


def cache_file(name: str) -> str:
    return os.path.join(cache_dir(), name)


def is_frozen() -> bool:
    """是否运行在打包后的可执行文件中（PyInstaller 等）。"""
    return bool(getattr(sys, "frozen", False))
