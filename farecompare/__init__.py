"""车票 / 机票 比价搜索 —— 本地运行，按最低价排序。

功能概览：
  * 火车票：gaotie.com.cn 车次通道（主）+ 12306 实时通道（补充）
  * 机票：同程旅行实时报价（主）+ 携程/去哪儿接口（可达时自动启用）+ 参考价兜底
  * 多线路中转（一次换乘、含隔夜）、航铁联程（飞机+高铁）
  * 日期区间搜索：一次看好几天的价格，直接挑最便宜那天
  * 城市/车站两级分组联想，自动过滤货运车站

全部数据在本机解析，不依赖任何外部 AI 服务。
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__", "main"]

APP_NAME = "车票 · 机票 比价搜索"
APP_SLUG = "fare-compare"


def main(argv: list[str] | None = None) -> int:
    """控制台入口（pip 安装后可直接执行 `farecompare`）。"""
    from .cli import main as _main
    return _main(argv)
