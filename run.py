"""直接运行入口（不安装也能用）：python run.py [serve|check|fetch ...]。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from farecompare.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
