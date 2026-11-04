"""打包成单文件 zipapp：离线环境只要有 python3 就能跑。

用法：
    python tools/build_pyz.py            # 生成 dist/chipdoc.pyz
    python dist/chipdoc.pyz doctor 手册.pdf
"""
from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def build() -> Path:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / "chipdoc.pyz"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shutil.copytree(ROOT / "chipdoc", tmp / "chipdoc", ignore=IGNORE)
        (tmp / "__main__.py").write_text(
            "from chipdoc.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
        zipapp.create_archive(tmp, target, interpreter="/usr/bin/env python3", compressed=True)
    return target


if __name__ == "__main__":
    path = build()
    print(f"{path}  ({path.stat().st_size / 1024:.0f} KB)")
